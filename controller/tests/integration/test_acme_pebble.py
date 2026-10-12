"""ACME DNS-01 issuance against a real ACME server (Pebble) with a fake DNS provider."""

import time
from collections.abc import Iterator

import httpx
import pytest
from testcontainers.core.container import DockerContainer

from switchyard.adapters.acme import AcmeError, AcmeIssuer, Dns01Solver, new_account_key
from switchyard.adapters.cloudflare import FakeCloudflare
from switchyard.services.certs import generate_key_and_csr, inspect_pem


def solver(dns: FakeCloudflare) -> Dns01Solver:
    async def resolve(domain: str) -> FakeCloudflare:
        return dns

    return Dns01Solver(resolve, wait_s=0)


@pytest.fixture(scope="module")
def directory_url() -> Iterator[str]:
    """Pebble with validation short-circuited (the DNS side is covered by the fake)."""
    container = (
        DockerContainer("ghcr.io/letsencrypt/pebble:latest")
        .with_env("PEBBLE_VA_ALWAYS_VALID", "1")
        .with_env("PEBBLE_VA_NOSLEEP", "1")
        .with_exposed_ports(14000)
    )
    with container:
        url = f"https://localhost:{container.get_exposed_port(14000)}/dir"
        for _ in range(60):
            try:
                if httpx.get(url, verify=False, timeout=2).status_code == 200:  # noqa: S501
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        yield url


async def test_issues_a_wildcard_and_apex_certificate_and_cleans_up_dns(directory_url: str) -> None:
    dns = FakeCloudflare()
    issuer = AcmeIssuer(directory_url, new_account_key(), solver(dns), verify=False)
    key_pem, csr_pem = generate_key_and_csr(("example.test", "*.example.test"))
    chain = await issuer.issue(csr_pem)
    info = inspect_pem(chain + key_pem)
    assert info.sans == ("*.example.test", "example.test")
    assert chain.count("BEGIN CERTIFICATE") >= 2, "the chain includes the issuing CA"
    assert [name for name, _ in dns.history] == ["_acme-challenge.example.test"] * 2
    assert dns.txt == {}, "TXT records are removed again"


async def test_a_reused_account_key_still_works(directory_url: str) -> None:
    key = new_account_key()
    for _ in range(2):
        issuer = AcmeIssuer(directory_url, key, solver(FakeCloudflare()), verify=False)
        _, csr_pem = generate_key_and_csr(("again.example.test",))
        assert "BEGIN CERTIFICATE" in await issuer.issue(csr_pem)


async def test_dns_failure_is_reported_and_leaves_no_records(directory_url: str) -> None:
    dns = FakeCloudflare()
    dns.fail_on.add("create_txt")
    issuer = AcmeIssuer(directory_url, new_account_key(), solver(dns), verify=False)
    _, csr_pem = generate_key_and_csr(("broken.example.test",))
    with pytest.raises(AcmeError, match="injected failure"):
        await issuer.issue(csr_pem)
    assert dns.txt == {}
