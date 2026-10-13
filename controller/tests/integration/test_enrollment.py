# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent enrollment: one-time tokens, certificates from the internal CA, renewal."""

import ipaddress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from cryptography import x509
from sqlalchemy import text

from switchyard.adapters.agent import BundleSigner
from switchyard.adapters.pki import LocalCa, PkiError, SignedCert
from switchyard.api.app import build_services, create_app
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.certs import generate_key_and_csr
from switchyard.services.engines import EngineService
from switchyard.services.enrollment import (
    ENROLL_TTL,
    EnrollmentError,
    EnrollmentService,
)
from switchyard.services.errors import NotFoundError, PermissionDeniedError
from tests.pipeline_env import GROUP, TENANT, World, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


async def register(w: World, name: str = "se-a", ip: str | None = "10.30.0.11") -> UUID:
    engine = await EngineService(w.db).register(
        principal(),
        TENANT,
        se_group_id=GROUP,
        name=name,
        dataplane_url=f"https://{name}.invalid:5556",
        mgmt_ip=ip,
    )
    return engine.id


def make(w: World, clock: Clock, pki: Any = None) -> tuple[EnrollmentService, LocalCa]:
    ca = LocalCa()
    svc = EnrollmentService(w.db, pki or ca, BundleSigner.generate().public_b64(), clock=clock)
    return svc, ca


def csr() -> str:
    return generate_key_and_csr(("whatever.example.test",))[1]


async def count(w: World, sql: str) -> int:
    async with admin_session(w.db.admin) as s:
        return int((await s.execute(text(sql))).scalar_one())


async def test_token_buys_one_certificate_with_names_from_the_engine_record(
    pg_url: str,
) -> None:
    async with world(pg_url) as w:
        engine_id = await register(w)
        svc, ca = make(w, Clock())
        token, _ = await svc.create_token(principal(), TENANT, engine_id)
        result = await svc.redeem(token, csr())
        cert = x509.load_pem_x509_certificate(result.cert_pem.encode())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert san.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("10.30.0.11")]
        assert san.get_values_for_type(x509.DNSName) == ["se-a"], "not the CSR's names"
        assert result.ca_pem == ca.ca_pem and result.bundle_public_key
        assert result.renewal_token.startswith("syren_")
        assert (
            await count(
                w,
                f"SELECT count(*) FROM service_engine WHERE agent_cert_serial = '{result.serial}'",
            )
            == 1
        )
        with pytest.raises(EnrollmentError):
            await svc.redeem(token, csr())  # single use
        assert (
            await count(w, "SELECT count(*) FROM audit_event WHERE action='engine.enrolled'") == 1
        )


async def test_renewal_tokens_rotate_and_expire(pg_url: str) -> None:
    async with world(pg_url) as w:
        engine_id = await register(w)
        clock = Clock()
        svc, _ = make(w, clock)
        token, _ = await svc.create_token(principal(), TENANT, engine_id)
        first = await svc.redeem(token, csr())
        second = await svc.redeem(first.renewal_token, csr())
        assert second.renewal_token != first.renewal_token
        with pytest.raises(EnrollmentError):
            await svc.redeem(first.renewal_token, csr())
        clock.now += timedelta(days=91)
        with pytest.raises(EnrollmentError):
            await svc.redeem(second.renewal_token, csr())
        assert (
            await count(w, "SELECT count(*) FROM audit_event WHERE action='engine.cert_renewed'")
            == 1
        )


async def test_expired_and_unknown_tokens_are_refused_alike(pg_url: str) -> None:
    async with world(pg_url) as w:
        engine_id = await register(w)
        clock = Clock()
        svc, _ = make(w, clock)
        token, _ = await svc.create_token(principal(), TENANT, engine_id)
        clock.now += ENROLL_TTL + timedelta(seconds=1)
        for bad in (token, "syenr_nope", "garbage"):
            with pytest.raises(EnrollmentError, match="invalid or expired"):
                await svc.redeem(bad, csr())


async def test_a_new_token_replaces_the_previous_one(pg_url: str) -> None:
    async with world(pg_url) as w:
        engine_id = await register(w)
        svc, _ = make(w, Clock())
        old, _ = await svc.create_token(principal(), TENANT, engine_id)
        new, _ = await svc.create_token(principal(), TENANT, engine_id)
        with pytest.raises(EnrollmentError):
            await svc.redeem(old, csr())
        assert (await svc.redeem(new, csr())).engine == "se-a"


async def test_signing_failures_do_not_burn_the_token(pg_url: str) -> None:
    async with world(pg_url) as w:
        engine_id = await register(w)

        class Flaky:
            def __init__(self, inner: LocalCa) -> None:
                self.inner, self.fail = inner, True

            async def sign(self, csr_pem: str, **kw: Any) -> SignedCert:
                if self.fail:
                    self.fail = False
                    raise PkiError("CA unavailable")
                return await self.inner.sign(csr_pem, **kw)

        svc, _ = make(w, Clock(), pki=Flaky(LocalCa()))
        token, _ = await svc.create_token(principal(), TENANT, engine_id)
        with pytest.raises(PkiError):
            await svc.redeem(token, csr())
        assert (await svc.redeem(token, csr())).serial
        with pytest.raises(PkiError, match="malformed"):
            await LocalCa().sign("junk", common_name="x", ip_sans=(), ttl=timedelta(days=1))


async def test_token_creation_rules(pg_url: str) -> None:
    async with world(pg_url) as w:
        svc, _ = make(w, Clock())
        no_ip = await register(w, "se-b", ip=None)
        with pytest.raises(ValueError, match="management address"):
            await svc.create_token(principal(), TENANT, no_ip)
        with pytest.raises(NotFoundError):
            await svc.create_token(principal(), TENANT, UUID(int=3))
        with pytest.raises(PermissionDeniedError):
            await svc.create_token(principal(Role.OPERATOR), TENANT, no_ip)


async def test_http_endpoints(pg_url: str) -> None:
    async with world(pg_url) as w:
        engine_id = await register(w)
        svc, _ = make(w, Clock())
        services = build_services(w.db, StaticEngineDirectory({}), enrollment=svc)
        root_token, _ = await services.tokens.create(
            principal(Role.SYSTEM_ADMIN, "root"), TENANT, "admin", Role.TENANT_ADMIN
        )
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            made = await client.post(
                f"/api/v1/tenants/{TENANT}/engines/{engine_id}/enrollment-token",
                headers={"Authorization": f"Bearer {root_token}"},
            )
            assert made.status_code == 201, made.text
            token = made.json()["token"]
            ok = await client.post("/api/v1/enroll", json={"token": token, "csr": csr()})
            assert ok.status_code == 200, ok.text
            body = ok.json()
            assert body["engine"] == "se-a" and "BEGIN CERTIFICATE" in body["certificate"]
            assert "PRIVATE KEY" not in ok.text
            again = await client.post("/api/v1/enroll", json={"token": token, "csr": csr()})
            assert again.status_code == 401
            assert again.headers["content-type"].startswith("application/problem+json")
            bad_csr = await client.post(
                "/api/v1/enroll", json={"token": body["renewal_token"], "csr": "junk"}
            )
            assert bad_csr.status_code == 422
            unauth = await client.post(
                f"/api/v1/tenants/{TENANT}/engines/{engine_id}/enrollment-token"
            )
            assert unauth.status_code == 401
        off = create_app(build_services(w.db, StaticEngineDirectory({})))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=off), base_url="http://api"
        ) as client:
            r = await client.post("/api/v1/enroll", json={"token": "x", "csr": "y"})
            assert r.status_code == 503


async def test_the_real_go_agent_enrolls_against_the_real_endpoint(
    pg_url: str, tmp_path: Path
) -> None:
    import asyncio
    import os
    import shutil
    import socket
    import subprocess

    import uvicorn

    if shutil.which("go") is None:
        pytest.skip("the Go toolchain is needed to build sy-agent")
    agent_dir = Path(__file__).parents[3] / "agent"
    binary = tmp_path / ("sy-agent.exe" if os.name == "nt" else "sy-agent")
    subprocess.run(["go", "build", "-o", str(binary), "./cmd/sy-agent"], cwd=agent_dir, check=True)
    async with world(pg_url) as w:
        engine_id = await register(w)
        svc, ca = make(w, Clock())
        token, _ = await svc.create_token(principal(), TENANT, engine_id)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = uvicorn.Config(
            create_app(build_services(w.db, StaticEngineDirectory({}), enrollment=svc)),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        try:
            while not server.started:
                await asyncio.sleep(0.05)
            agent_state = tmp_path / "agent"
            url = f"http://127.0.0.1:{port}"

            async def run(*args: str) -> tuple[int, str]:
                done = await asyncio.to_thread(
                    subprocess.run,
                    [str(binary), *args],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return done.returncode, done.stdout + done.stderr

            code, output = await run(
                "enroll",
                "--controller",
                url,
                "--token",
                token,
                "--dir",
                str(agent_state),
                "--insecure-plain-http",
            )
            assert code == 0, output
            cert = x509.load_pem_x509_certificate((agent_state / "tls.crt").read_bytes())
            assert (agent_state / "ca.crt").read_text() == ca.ca_pem
            assert cert.subject.rfc4514_string() == "CN=se-a"
            assert (agent_state / "bundle.pub").read_text().strip()
            renew = (agent_state / "renew.token").read_text().strip()
            code, output = await run(
                "renew", "--controller", url, "--dir", str(agent_state), "--insecure-plain-http"
            )
            assert code == 0, output
            assert (agent_state / "renew.token").read_text().strip() != renew
            code, output = await run(
                "enroll",
                "--controller",
                url,
                "--token",
                token,
                "--dir",
                str(tmp_path / "other"),
                "--insecure-plain-http",
            )
            assert code != 0 and "401" in output
        finally:
            server.should_exit = True
            await task


async def test_an_engine_named_like_the_controller_gets_no_certificate(pg_url: str) -> None:
    """Agents accept the controller's name as caller: no tenant-named engine may hold it."""
    async with world(pg_url) as w:
        for name, extra in [("switchyard-controller", ()), ("ops-ctl", ("ops-ctl",))]:
            custom = EnrollmentService(
                w.db, LocalCa(), BundleSigner.generate().public_b64(), reserved_names=extra
            )
            engine_id = await register(w, name=name, ip="10.30.0.12" if extra else "10.30.0.11")
            token, _ = await custom.create_token(principal(), TENANT, engine_id)
            with pytest.raises(EnrollmentError):
                await custom.redeem(token, csr())
