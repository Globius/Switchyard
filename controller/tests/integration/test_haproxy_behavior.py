"""Send real requests through rendered HAProxy config and check what backends see.

``haproxy -c`` proves the syntax; this proves the semantics the renderer promises: routing order,
single-label wildcards, redirects, header hygiene and client-IP restoration. Everything runs on a
throwaway Docker network that uses the RFC 5737 range 192.0.2.0/24:

- 192.0.2.20  edge: HAProxy running the rendered config
- 192.0.2.51-53  backends A, B and C: HAProxy answering with what it received
- 192.0.2.98/99  clients (curl); only .99 is in the allow list
"""

import io
import tarfile
import uuid
from collections.abc import Iterator
from typing import Any

import docker
import pytest

from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.render.haproxy import render_haproxy
from tests.haproxy_check import HAPROXY_IMAGE

CURL_IMAGE = "curlimages/curl:8.10.1"
SUBNET = "192.0.2.0/24"
EDGE = "192.0.2.20"
BACKEND_CFG = """
global
    maxconn 100
defaults
    mode http
    timeout client 5s
    timeout server 5s
    timeout connect 5s
frontend f
    bind :8080
    http-request return status 200 content-type text/plain lf-string "be={label} host=%[req.hdr(host)] xff=%[req.hdr(x-forwarded-for)] proto=%[req.hdr(x-forwarded-proto)] cf=%[req.hdr(cf-connecting-ip)] path=%[path]"
"""


def data() -> dict[str, Any]:
    def pool(pid: str, ip: str) -> dict[str, Any]:
        return {
            "id": pid,
            "tenant_id": "t",
            "name": pid,
            "members": [{"address": ip, "port": 8080}],
        }

    def https_vs(vid: str, port: int, **extra: Any) -> dict[str, Any]:
        return {
            "id": vid,
            "tenant_id": "t",
            "se_group_id": "g",
            "name": vid,
            "protocol": "http",
            "vip": EDGE,
            "ports": [{"port": port, "tls": True}],
            "certificate_ids": ["c"],
            "default_pool_id": "c-pool",
            **extra,
        }

    return {
        "tenants": [{"id": "t", "name": "acme"}],
        "networks": [{"id": "n", "tenant_id": "t", "name": "lab", "cidr": SUBNET}],
        "se_groups": [
            {
                "id": "g",
                "tenant_id": "t",
                "name": "edge",
                "ha_mode": "vrrp",
                "networks": [{"network_id": "n", "role": "vip"}],
            }
        ],
        "certificates": [
            {
                "id": "c",
                "tenant_id": "t",
                "name": "lab",
                "source": "upload",
                "sans": ["*.example.test", "*.apps.example.test", "app.example.test"],
                "not_after": "2038-01-01T00:00:00+00:00",
                "secret_ref": "kv/lab",
            }
        ],
        "pools": [
            pool("a-pool", "192.0.2.51"),
            pool("b-pool", "192.0.2.52"),
            pool("c-pool", "192.0.2.53"),
        ],
        "policy_sets": [
            {
                "id": "dav",
                "tenant_id": "t",
                "name": "dav",
                "kind": "redirect",
                "rules": [
                    {
                        "match": {
                            "host": "app.example.test",
                            "path_prefix": "/.well-known/carddav",
                        },
                        "redirect": {"location": "/remote.php/dav/", "code": 301},
                    }
                ],
            },
            {
                "id": "cf",
                "tenant_id": "t",
                "name": "cf",
                "kind": "allow_list",
                "cidrs": ["192.0.2.99/32"],
            },
        ],
        "virtual_services": [
            https_vs(
                "main",
                443,
                ports=[{"port": 443, "tls": True}, {"port": 80, "https_redirect_port": 443}],
                policy_set_ids=["dav"],
                routes=[
                    {"match": {"host": "app.example.test"}, "pool_id": "a-pool"},
                    {
                        "match": {"host": "app.example.test", "path_prefix": "/api"},
                        "pool_id": "b-pool",
                        "order": 1,
                    },
                    {"match": {"host": "*.apps.example.test"}, "pool_id": "b-pool"},
                ],
            ),
            https_vs(
                "cf-enforced",
                8443,
                allow_list_id="cf",
                enforce_allow_list=True,
                restore_client_ip_header="CF-Connecting-IP",
                routes=[{"match": {"host": "app.example.test"}, "pool_id": "a-pool"}],
            ),
            https_vs(
                "cf-open",
                9443,
                allow_list_id="cf",
                restore_client_ip_header="CF-Connecting-IP",
                routes=[{"match": {"host": "app.example.test"}, "pool_id": "a-pool"}],
            ),
        ],
    }


def tar_of(files: dict[str, str], prefix: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, content in files.items():
            raw = content.encode()
            info = tarfile.TarInfo(f"{prefix}{path}")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


EDGE_START = r"""
set -eu
mkdir -p /etc/switchyard/certs /run/haproxy
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout /tmp/k.pem -out /tmp/c.pem -subj /CN=lab -days 1 2>/dev/null
cat /tmp/c.pem /tmp/k.pem > /etc/switchyard/certs/lab.pem
exec haproxy -f /etc/switchyard/haproxy/haproxy.cfg
"""


@pytest.fixture(scope="module")
def lab() -> Iterator[docker.DockerClient]:
    client = docker.from_env()
    name = f"sy-lab-{uuid.uuid4().hex[:8]}"
    pool = docker.types.IPAMPool(subnet=SUBNET, gateway="192.0.2.254")
    network = client.networks.create(name, ipam=docker.types.IPAMConfig(pool_configs=[pool]))
    started = []

    def run(
        image: str, ip: str, command: Any, files: dict[str, str] | None = None, **kw: Any
    ) -> Any:
        api = client.api
        cfg = api.create_networking_config({name: api.create_endpoint_config(ipv4_address=ip)})
        created = api.create_container(
            image,
            command=command,
            detach=True,
            networking_config=cfg,
            host_config=api.create_host_config(network_mode=name),
            **kw,
        )
        container = client.containers.get(created["Id"])
        if files:
            container.put_archive("/", tar_of(files, ""))
        container.start()
        started.append(container)
        return container

    try:
        for label, ip in (("A", "192.0.2.51"), ("B", "192.0.2.52"), ("C", "192.0.2.53")):
            cfg = BACKEND_CFG.format(label=label)
            run(HAPROXY_IMAGE, ip, ["haproxy", "-f", "/be.cfg"], {"be.cfg": cfg})
        snapshot = snapshot_from_mapping(data())
        rendered = render_haproxy(snapshot, __import__("uuid").UUID(symbolic_uuid("g")))
        files = {f"etc/switchyard/haproxy/{p}": c for p, c in rendered.files.items()}
        run(HAPROXY_IMAGE, EDGE, ["sh", "-c", EDGE_START], files, user="root")
        yield client
    finally:
        for container in started:
            container.remove(force=True)
        network.remove()


def curl(lab: docker.DockerClient, ip: str, script: str) -> dict[str, str]:
    """Run ``script`` in a curl container with address ``ip``; return ``label -> output``."""
    network = next(n for n in lab.networks.list() if n.name.startswith("sy-lab-"))
    api = lab.api
    cfg = api.create_networking_config({network.name: api.create_endpoint_config(ipv4_address=ip)})
    prelude = (
        "req() { label=$1; shift; out=$(curl -s -k --max-time 6 -w '|%{http_code}|%{redirect_url}' \"$@\") "
        '&& echo "$label: $out" || echo "$label: FAIL($?)"; }\n'
    )
    created = api.create_container(
        CURL_IMAGE,
        entrypoint=["sh", "-c"],
        command=[prelude + script],
        networking_config=cfg,
        host_config=api.create_host_config(network_mode=network.name),
    )
    container = lab.containers.get(created["Id"])
    try:
        container.start()
        container.wait()
        text = container.logs().decode()
    finally:
        container.remove(force=True)
    return dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)


def resolve(host: str, port: int) -> str:
    return f"--resolve {host}:{port}:{EDGE}"


@pytest.fixture(scope="module")
def main_results(lab: docker.DockerClient) -> dict[str, str]:
    script = "\n".join(
        [
            f"req exact {resolve('app.example.test', 443)} https://app.example.test/",
            f"req api {resolve('app.example.test', 443)} https://app.example.test/api/v1",
            f"req wild {resolve('x.apps.example.test', 443)} https://x.apps.example.test/",
            f"req deep {resolve('a.b.apps.example.test', 443)} https://a.b.apps.example.test/",
            f"req apex {resolve('apps.example.test', 443)} https://apps.example.test/",
            f"req unknown {resolve('nope.example.test', 443)} https://nope.example.test/",
            f"req caps {resolve('app.example.test', 443)} -H 'Host: APP.Example.Test:443' https://app.example.test/",
            f"req http {resolve('app.example.test', 80)} http://app.example.test/some/path?q=1",
            f"req dav {resolve('app.example.test', 443)} https://app.example.test/.well-known/carddav/x",
            f"req spoof {resolve('app.example.test', 443)} -H 'X-Forwarded-For: 1.2.3.4' -H 'X-Forwarded-Proto: http' https://app.example.test/",
        ]
    )
    return curl(lab, "192.0.2.99", script)


class TestMainService:
    def test_exact_host_goes_to_its_pool_with_forwarding_headers(
        self, main_results: dict[str, str]
    ) -> None:
        assert main_results["exact"] == (
            "be=A host=app.example.test xff=192.0.2.99 proto=https cf= path=/|200|"
        )

    def test_path_route_beats_host_only_route(self, main_results: dict[str, str]) -> None:
        assert main_results["api"].startswith("be=B ")

    def test_wildcard_matches_exactly_one_label(self, main_results: dict[str, str]) -> None:
        assert main_results["wild"].startswith("be=B ")
        assert main_results["deep"].startswith("be=C "), "two labels deep must fall to default"
        assert main_results["apex"].startswith("be=C "), "apex is not covered by *."

    def test_unknown_host_goes_to_default_pool(self, main_results: dict[str, str]) -> None:
        assert main_results["unknown"].startswith("be=C ")

    def test_host_matching_ignores_case_and_port(self, main_results: dict[str, str]) -> None:
        assert main_results["caps"].startswith("be=A ")

    def test_http_redirects_to_https_keeping_path_and_query(
        self, main_results: dict[str, str]
    ) -> None:
        assert main_results["http"] == "|301|https://app.example.test/some/path?q=1"

    def test_policy_set_redirect(self, main_results: dict[str, str]) -> None:
        assert main_results["dav"].endswith("|301|https://app.example.test/remote.php/dav/")

    def test_client_supplied_forwarding_headers_are_discarded(
        self, main_results: dict[str, str]
    ) -> None:
        assert " xff=192.0.2.99 proto=https " in main_results["spoof"]


@pytest.fixture(scope="module")
def cf_results(lab: docker.DockerClient) -> dict[str, dict[str, str]]:
    hdr = "-H 'CF-Connecting-IP: 203.0.113.9'"
    script = "\n".join(
        [
            f"req enforced {resolve('app.example.test', 8443)} {hdr} https://app.example.test:8443/",
            f"req open {resolve('app.example.test', 9443)} {hdr} https://app.example.test:9443/",
        ]
    )
    return {"trusted": curl(lab, "192.0.2.99", script), "other": curl(lab, "192.0.2.98", script)}


class TestClientAddressRestoration:
    def test_trusted_source_has_its_client_address_restored(
        self, cf_results: dict[str, dict[str, str]]
    ) -> None:
        assert " xff=203.0.113.9 " in cf_results["trusted"]["enforced"]
        assert " xff=203.0.113.9 " in cf_results["trusted"]["open"]

    def test_untrusted_source_is_rejected_when_enforced(
        self, cf_results: dict[str, dict[str, str]]
    ) -> None:
        assert cf_results["other"]["enforced"].startswith("FAIL(")

    def test_untrusted_source_cannot_spoof_the_header(
        self, cf_results: dict[str, dict[str, str]]
    ) -> None:
        out = cf_results["other"]["open"]
        assert " xff=192.0.2.98 " in out
        assert " cf= " in out, "the spoofed header must be removed before it reaches the backend"
