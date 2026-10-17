# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The engine's Vector ships to Loki, OTLP, both or neither, from ``vector.env`` alone (ADR 0221).

``se-image/files/sy-vector-sinks`` writes the sinks the environment asks for and ``vector validate`` loads them with
``se-image/files/vector.yaml``, as the unit does. The end-to-end case swaps the journald sources for files, points
both sinks at one small receiver on a private Docker network and checks that a single HAProxy line arrives at each
with the documented resource attributes.
"""

import gzip
import io
import json
import re
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import docker
import pytest

VECTOR_IMAGE = "timberio/vector:0.46.1-debian"
RECEIVER_IMAGE = "python:3.12-slim"
FILES = Path(__file__).parents[3] / "se-image" / "files"
ENGINE_ENV = {
    "TENANT_ID": "t-acme",
    "TENANT_NAME": "acme",
    "SE_GROUP": "edge",
    "ENGINE": "engine-1",
    "VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION": "true",
}
LINE = (
    "192.0.2.99:41234 [16/Oct/2026:16:00:00.123] vs_web be_pool/member1 0/0/1/2/3 404 150 - - ---- "
    '1/1/0/0/0 0/0 "GET /probe-1 HTTP/1.1"'
)
RECEIVER = r"""
import http.server, json, threading, time
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with open("/out/requests.jsonl", "a") as f:
            f.write(json.dumps({"port": self.server.server_port, "path": self.path,
                                "headers": dict(self.headers), "body": body.hex()}) + "\n")
        self.send_response(204); self.end_headers()
    def log_message(self, *a): pass
for port in (3100, 4318):
    s = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
print("ready", flush=True)
time.sleep(600)
"""
LOKI = {"LOKI_URL": "http://loki.example.org:3100"}
OTLP = {"OTLP_LOGS_URL": "https://collector.example.org:4318"}


def tar(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, text in files.items():
            raw = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


def wait_for(container: Any, deadline_s: float = 120) -> tuple[int, str]:
    deadline = time.monotonic() + deadline_s
    while container.reload() or container.status == "running":
        if time.monotonic() > deadline:
            raise TimeoutError("container did not finish")
        time.sleep(0.5)
    return int(container.attrs["State"]["ExitCode"]), container.logs().decode(errors="replace")


def validate(env: dict[str, str]) -> tuple[int, str]:
    """Runs the sinks script, then ``vector validate`` on the image config plus what it wrote."""
    container = docker.from_env().containers.create(
        VECTOR_IMAGE,
        entrypoint=["sh", "-c"],
        command=[
            "mkdir -p /r && sh /cfg/sy-vector-sinks /r/sinks.yaml && "
            "vector validate --no-environment --config-dir /r /cfg/vector.yaml"
        ],
        environment={**ENGINE_ENV, **env},
    )
    try:
        container.put_archive(
            "/",
            tar(
                {
                    "cfg/vector.yaml": (FILES / "vector.yaml").read_text("utf-8"),
                    "cfg/sy-vector-sinks": (FILES / "sy-vector-sinks").read_text("utf-8"),
                }
            ),
        )
        container.start()
        return wait_for(container)
    finally:
        container.remove(force=True)


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="neither"),
        pytest.param(LOKI, id="loki-only"),
        pytest.param(OTLP, id="otlp-only"),
        pytest.param({**LOKI, **OTLP}, id="both"),
        pytest.param(
            {
                **OTLP,
                "OTLP_LOGS_HEADERS": "Authorization: Bearer x;X-Org: 7",
                "OTLP_LOGS_CA_FILE": "/etc/hosts",
            },
            id="otlp-headers-and-ca",
        ),
        pytest.param({**OTLP, "OTLP_LOGS_INSECURE_SKIP_VERIFY": "true"}, id="otlp-skip-verify"),
    ],
)
def test_vector_validates_for_each_sink_combination(env: dict[str, str]) -> None:
    code, output = validate(env)
    assert code == 0, output


@pytest.mark.parametrize(
    "env",
    [
        {**OTLP, "OTLP_LOGS_HEADERS": 'X: "quoted'},
        {**OTLP, "OTLP_LOGS_HEADERS": "no colon here"},
        {**OTLP, "OTLP_LOGS_HEADERS": "bad name: v"},
        {"LOKI_URL": "http://loki.example.org/${HOME}"},
    ],
)
def test_the_sinks_script_refuses_values_that_could_break_the_config(env: dict[str, str]) -> None:
    code, output = validate(env)
    assert code != 0
    assert "sy-vector-sinks" in output


@pytest.fixture
def network() -> Iterator[Any]:
    net = docker.from_env().networks.create(f"sy-otlp-{int(time.time())}")
    try:
        yield net
    finally:
        net.remove()


def test_one_haproxy_line_reaches_both_sinks_with_the_expected_attributes(network: Any) -> None:
    client = docker.from_env()
    receiver = client.containers.run(
        RECEIVER_IMAGE,
        ["sh", "-c", 'mkdir -p /out && python -c "$RECEIVER_CODE"'],
        environment={"RECEIVER_CODE": RECEIVER},
        network=network.name,
        name=f"{network.name}-receiver",
        detach=True,
    )
    # Journald is not there in a container: the three sources read files, the transforms stay as shipped.
    config = re.sub(
        r"type: journald\n    include_units: \[(haproxy|coraza-spoa|sy-auth)\.service\]\n(    current_boot_only: true\n)?",
        lambda m: f"type: file\n    include: [/in/{m.group(1)}.log]\n    read_from: beginning\n",
        (FILES / "vector.yaml").read_text("utf-8"),
    )
    vector = client.containers.create(
        VECTOR_IMAGE,
        entrypoint=["sh", "-c"],
        command=[
            "mkdir -p /r && sh /cfg/sy-vector-sinks /r/sinks.yaml && "
            "exec vector --config /cfg/vector.yaml --config-dir /r"
        ],
        environment={
            **ENGINE_ENV,
            "LOKI_URL": f"http://{receiver.name}:3100",
            "OTLP_LOGS_URL": f"http://{receiver.name}:4318/",
            "OTLP_LOGS_HEADERS": "Authorization: Bearer test-token",
        },
        network=network.name,
    )
    try:
        deadline = time.monotonic() + 90
        while "ready" not in receiver.logs().decode() and time.monotonic() < deadline:
            time.sleep(0.5)
        vector.put_archive(
            "/",
            tar(
                {
                    "cfg/vector.yaml": config,
                    "cfg/sy-vector-sinks": (FILES / "sy-vector-sinks").read_text("utf-8"),
                    "in/haproxy.log": LINE + "\n",
                    "in/coraza-spoa.log": "",
                    "in/sy-auth.log": "",
                }
            ),
        )
        vector.start()
        requests: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            _, raw = receiver.exec_run(["sh", "-c", "cat /out/requests.jsonl 2>/dev/null || true"])
            requests = [json.loads(x) for x in raw.decode().splitlines() if x]
            if {r["port"] for r in requests} == {3100, 4318}:
                break
            time.sleep(1)
        assert {r["port"] for r in requests} == {3100, 4318}, vector.logs().decode(errors="replace")

        loki = next(r for r in requests if r["port"] == 3100)
        assert loki["path"] == "/loki/api/v1/push"
        loki_body = bytes.fromhex(loki["body"])  # snappy-framed protobuf: the short label text stays readable
        assert b"acme" in loki_body and b"vs_web" in loki_body

        otlp = next(r for r in requests if r["port"] == 4318)
        assert otlp["path"] == "/v1/logs"
        headers = {k.lower(): v for k, v in otlp["headers"].items()}
        assert headers["authorization"] == "Bearer test-token"
        assert headers["content-type"] == "application/json"
        body = bytes.fromhex(otlp["body"])
        if headers.get("content-encoding") == "gzip":
            body = gzip.decompress(body)
        resource = json.loads(body)["resourceLogs"][0]
        attrs = {a["key"]: a["value"]["stringValue"] for a in resource["resource"]["attributes"]}
        assert attrs == {
            "service.name": "haproxy",
            "tenant": "t-acme",
            "tenant_name": "acme",
            "se_group": "edge",
            "engine": "engine-1",
            "frontend": "vs_web",
        }
        record = resource["scopeLogs"][0]["logRecords"][0]
        assert "GET /probe-1 HTTP/1.1" in record["body"]["stringValue"]
    finally:
        vector.remove(force=True)
        receiver.remove(force=True)
