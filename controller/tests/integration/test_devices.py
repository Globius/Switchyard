"""Device posture: enrollment, signed reports, certificates, revocation and isolation (ADR 0196)."""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID
from sqlalchemy import text

from switchyard.adapters.pki import LocalCa, PkiError, SignedCert
from switchyard.api.app import build_services, create_app
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.certs import generate_key_and_csr
from switchyard.services.devices import (
    TOKEN_TTL,
    DeviceAuthError,
    DeviceRequestError,
    DeviceService,
    canonical,
)
from switchyard.services.errors import NotFoundError, PermissionDeniedError
from tests.pipeline_env import TENANT, World, principal, world, world_data

pytestmark = pytest.mark.usefixtures("owner_engine")

POLICY = UUID("00000000-0000-0000-0000-0000000000a1")
GOOD = {
    "os": "windows",
    "os_version": "10.0.22631",
    "disk_encrypted": True,
    "firewall_enabled": True,
    "days_since_update": 5,
}


def data() -> dict[str, Any]:
    d = world_data()
    d["policy_sets"] = [
        *d.get("policy_sets", []),
        {
            "id": str(POLICY),
            "tenant_id": "t-acme",
            "name": "managed",
            "kind": "posture",
            "pki_role": "acme-devices",
            "cert_ttl_h": 12,
            "requirements": {
                "minimums": [{"os": "windows", "version": "10.0.19045"}],
                "max_patch_age_days": 30,
            },
        },
    ]
    return d


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def ms(self, plus: int = 0) -> int:
        return int(self.now.timestamp() * 1000) + plus


class Device:
    """What the agent holds: an Ed25519 key, a CSR, and the knowledge of how to sign."""

    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        raw = self.key.public_key().public_bytes_raw()
        self.public = base64.b64encode(raw).decode()
        self.csr = generate_key_and_csr(("ignored.example.test",))[1]
        self.id: UUID | None = None

    def sign(self, body: bytes, ts: int, path: str = "/api/v1/device/report") -> str:
        return base64.b64encode(self.key.sign(canonical("POST", path, ts, body))).decode()

    def body(self, facts: dict[str, Any] | None = None, *, csr: bool = True) -> bytes:
        document: dict[str, Any] = {"facts": facts or GOOD}
        if csr:
            document["csr"] = self.csr
        return json.dumps(document).encode()


def make(w: World, clock: Clock, signer: Any = None) -> tuple[DeviceService, LocalCa]:
    ca = LocalCa()
    return DeviceService(w.db, lambda role: signer or ca, clock=clock), ca


async def enrolled(svc: DeviceService, name: str = "laptop-ana") -> Device:
    token, _ = await svc.create_enrollment(principal(), TENANT, POLICY, "Ana")
    device = Device()
    result = await svc.enroll(token, name, device.public, "ana@example.test")
    device.id = result.device_id
    assert result.policy == "managed" and result.cert_ttl_h == 12 and result.tenant == "acme"
    return device


async def report(
    svc: DeviceService, d: Device, clock: Clock, facts: dict[str, Any] | None = None, **kw: Any
) -> Any:
    body = kw.pop("body", None) or d.body(facts)
    ts = kw.pop("ts", clock.ms())
    assert d.id is not None
    return await svc.report(
        d.id,
        timestamp_ms=ts,
        signature=kw.pop("signature", None) or d.sign(body, ts),
        method="POST",
        path="/api/v1/device/report",
        body=body,
    )


async def count(w: World, sql: str) -> int:
    async with admin_session(w.db.admin) as s:
        return int((await s.execute(text(sql))).scalar_one())


async def test_a_compliant_device_gets_a_short_lived_client_certificate(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        clock = Clock()
        svc, ca = make(w, clock)
        d = await enrolled(svc)
        result = await report(svc, d, clock)
        assert result.compliant and result.failures == []
        assert result.ca == ca.ca_pem and result.next_report_s == 4 * 3600
        cert = x509.load_pem_x509_certificate((result.certificate or "").encode())
        assert cert.subject.rfc4514_string() == "CN=laptop-ana"
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert list(eku) == [ExtendedKeyUsageOID.CLIENT_AUTH], (
            "a device cert is never a server cert"
        )
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert san.get_values_for_type(x509.UniformResourceIdentifier) == [
            f"urn:switchyard:device:acme:{d.id}"
        ]
        assert not san.get_values_for_type(x509.DNSName), "names come from the record, not the CSR"
        life = cert.not_valid_after_utc - cert.not_valid_before_utc
        assert timedelta(hours=11) < life < timedelta(hours=13)
        assert result.not_after == clock.now + timedelta(hours=12)
        assert (
            await count(w, "SELECT count(*) FROM audit_event WHERE action='device.enrolled'") == 1
        )
        assert (
            await count(w, "SELECT count(*) FROM audit_event WHERE action='device.compliant'") == 1
        )
        row = await svc.get_device(principal(Role.VIEWER), TENANT, d.id or UUID(int=0))
        assert row.compliant and row.cert_serial and row.facts and row.facts["os"] == "windows"


async def test_failing_facts_get_no_certificate_and_flips_are_audited(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        clock = Clock()
        svc, _ = make(w, clock)
        d = await enrolled(svc)
        bad = {
            **GOOD,
            "os_version": "10.0.19044",
            "firewall_enabled": False,
            "days_since_update": 90,
        }
        clock.now += timedelta(seconds=1)
        result = await report(svc, d, clock, bad)
        assert not result.compliant and result.certificate is None
        assert result.failures == ["os_too_old", "firewall_off", "patch_too_old"]
        clock.now += timedelta(seconds=1)
        again = await report(svc, d, clock, bad)  # no new audit line for the same verdict
        assert not again.compliant
        clock.now += timedelta(seconds=1)
        assert (await report(svc, d, clock)).compliant
        actions = [r async for r in _actions(w)]
        assert actions == ["device.non_compliant", "device.compliant"]


async def _actions(w: World) -> Any:
    async with admin_session(w.db.admin) as s:
        rows = await s.execute(
            text(
                "SELECT action FROM audit_event WHERE action LIKE 'device.%' "
                "AND action IN ('device.compliant','device.non_compliant') ORDER BY at, id"
            )
        )
        for (action,) in rows:
            yield action


async def test_no_csr_means_no_certificate_but_the_verdict(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        clock = Clock()
        svc, _ = make(w, clock)
        d = await enrolled(svc)
        result = await report(svc, d, clock, body=d.body(csr=False))
        assert result.compliant and result.certificate is None


async def test_replays_skew_forgeries_and_tampering_are_refused_alike(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        clock = Clock()
        svc, _ = make(w, clock)
        d = await enrolled(svc)
        ts = clock.ms()
        body = d.body()
        await report(svc, d, clock, ts=ts, body=body)
        with pytest.raises(DeviceAuthError):  # the same signed report again
            await report(svc, d, clock, ts=ts, body=body)
        with pytest.raises(DeviceAuthError):  # an older one
            await report(svc, d, clock, ts=ts - 1000)
        with pytest.raises(DeviceAuthError):  # too far in the future
            await report(svc, d, clock, ts=ts + 6 * 60 * 1000)
        with pytest.raises(DeviceAuthError):  # signed by another key
            await report(svc, d, clock, ts=ts + 10, signature=Device().sign(body, ts + 10))
        with pytest.raises(DeviceAuthError):  # the body changed after signing
            await report(
                svc,
                d,
                clock,
                ts=ts + 20,
                signature=d.sign(body, ts + 20),
                body=d.body({**GOOD, "disk_encrypted": False}),
            )
        d_ts = ts + 30
        with pytest.raises(DeviceAuthError):  # signed for another path
            await svc.report(
                d.id or UUID(int=0),
                timestamp_ms=d_ts,
                signature=d.sign(body, d_ts, "/other"),
                method="POST",
                path="/api/v1/device/report",
                body=body,
            )
        with pytest.raises(DeviceAuthError):  # unknown device
            await svc.report(
                UUID(int=9),
                timestamp_ms=ts + 40,
                signature="AAAA",
                method="POST",
                path="/x",
                body=b"",
            )
        with pytest.raises(DeviceAuthError):  # not base64
            await report(svc, d, clock, ts=ts + 50, signature="!!!")


async def test_unusable_reports(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        clock = Clock()
        svc, _ = make(w, clock)
        d = await enrolled(svc)
        for i, bad in enumerate(
            (
                b"not json",
                b"[]",
                json.dumps({"facts": GOOD, "extra": 1}).encode(),
                json.dumps({"facts": {**GOOD, "root": True}}).encode(),
                json.dumps({"facts": {**GOOD, "os": "beos"}}).encode(),
                json.dumps({"facts": GOOD, "csr": 5}).encode(),
                b"x" * 40_000,
            ),
            start=1,
        ):
            with pytest.raises(DeviceRequestError):
                await report(svc, d, clock, ts=clock.ms(i), body=bad)


async def test_enrollment_rules(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        clock = Clock()
        svc, _ = make(w, clock)
        token, _ = await svc.create_enrollment(principal(), TENANT, POLICY)
        device = Device()
        await svc.enroll(token, "one", device.public)
        with pytest.raises(DeviceAuthError, match="invalid or expired"):
            await svc.enroll(token, "two", device.public)  # single use
        for bad in ("garbage", "sydev_nope"):
            with pytest.raises(DeviceAuthError, match="invalid or expired"):
                await svc.enroll(bad, "x", device.public)
        old, _ = await svc.create_enrollment(principal(), TENANT, POLICY)
        clock.now += TOKEN_TTL + timedelta(seconds=1)
        with pytest.raises(DeviceAuthError):
            await svc.enroll(old, "late", device.public)
        clock.now = datetime.now(UTC)
        fresh, _ = await svc.create_enrollment(principal(), TENANT, POLICY)
        with pytest.raises(DeviceRequestError, match="already enrolled"):
            await svc.enroll(fresh, "one", device.public)  # name taken; the token is not burned
        with pytest.raises(DeviceRequestError, match="Ed25519"):
            await svc.enroll(fresh, "three", "AAAA")
        for name in ("", "a b", "-x", "x" * 64, "é"):
            with pytest.raises(DeviceRequestError, match="name"):
                await svc.enroll(fresh, name, device.public)
        await svc.enroll(fresh, "three", device.public)


async def test_revoked_devices_cannot_report_and_removal_frees_the_name(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        clock = Clock()
        svc, _ = make(w, clock)
        d = await enrolled(svc)
        await report(svc, d, clock)
        await svc.revoke(principal(), TENANT, d.id or UUID(int=0))
        clock.now += timedelta(seconds=2)
        with pytest.raises(DeviceAuthError):
            await report(svc, d, clock)
        row = await svc.get_device(principal(), TENANT, d.id or UUID(int=0))
        assert row.status == "revoked" and row.compliant is False and row.revoked_at
        await svc.revoke(principal(), TENANT, d.id or UUID(int=0), remove=True)
        with pytest.raises(NotFoundError):
            await svc.get_device(principal(), TENANT, d.id or UUID(int=0))
        await enrolled(svc)  # the name is free again


async def test_roles_tenants_and_row_level_security(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        clock = Clock()
        svc, _ = make(w, clock)
        d = await enrolled(svc)
        with pytest.raises(PermissionDeniedError):
            await svc.create_enrollment(principal(Role.OPERATOR), TENANT, POLICY)
        with pytest.raises(PermissionDeniedError):
            await svc.revoke(principal(Role.OPERATOR), TENANT, d.id or UUID(int=0))
        assert len(await svc.list_devices(principal(Role.VIEWER), TENANT)) == 1
        other = UUID(int=77)
        with pytest.raises(PermissionDeniedError):
            await svc.list_devices(principal(tenant=other), TENANT)
        # a principal of another tenant sees nothing even when it asks for its own tenant
        assert await svc.list_devices(principal(tenant=other), other) == []
        with pytest.raises(NotFoundError):
            await svc.get_device(principal(tenant=other), other, d.id or UUID(int=0))
        with pytest.raises(NotFoundError):
            await svc.create_enrollment(principal(), TENANT, UUID(int=5))  # not a posture set


async def test_a_ca_failure_leaves_the_device_usable_and_nothing_issued(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        clock = Clock()

        class Down:
            async def sign(self, csr_pem: str, **kw: Any) -> SignedCert:
                raise PkiError("CA unavailable")

        svc, _ = make(w, clock, signer=Down())
        d = await enrolled(svc)
        with pytest.raises(PkiError):
            await report(svc, d, clock)
        row = await svc.get_device(principal(), TENANT, d.id or UUID(int=0))
        assert row.cert_serial is None
        none = DeviceService(w.db, lambda role: None, clock=clock)
        clock.now += timedelta(seconds=2)
        with pytest.raises(DeviceRequestError, match="not configured"):
            await report(none, d, clock)
        clock.now += timedelta(seconds=2)
        assert (await report(none, d, clock, body=d.body(csr=False))).compliant


async def test_http_endpoints(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        clock = Clock()
        svc, _ = make(w, clock)
        services = build_services(w.db, StaticEngineDirectory({}), devices=svc)
        admin, _ = await services.tokens.create(
            principal(Role.SYSTEM_ADMIN, "root"), TENANT, "admin", Role.TENANT_ADMIN
        )
        viewer, _ = await services.tokens.create(
            principal(Role.SYSTEM_ADMIN, "root"), TENANT, "viewer", Role.VIEWER
        )
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            auth = {"Authorization": f"Bearer {admin}"}
            made = await client.post(
                f"/api/v1/tenants/{TENANT}/device-enrollments",
                json={"policy_id": str(POLICY), "label": "Ana"},
                headers=auth,
            )
            assert made.status_code == 201, made.text
            denied = await client.post(
                f"/api/v1/tenants/{TENANT}/device-enrollments",
                json={"policy_id": str(POLICY)},
                headers={"Authorization": f"Bearer {viewer}"},
            )
            assert denied.status_code == 403
            device = Device()
            enrolled_ = await client.post(
                "/api/v1/device/enroll",
                json={
                    "token": made.json()["token"],
                    "name": "laptop-ana",
                    "public_key": device.public,
                },
            )
            assert enrolled_.status_code == 200, enrolled_.text
            device.id = UUID(enrolled_.json()["device_id"])
            body = device.body()
            ts = clock.ms()
            ok = await client.post(
                "/api/v1/device/report",
                content=body,
                headers={
                    "X-Device-Id": str(device.id),
                    "X-Device-Timestamp": str(ts),
                    "X-Device-Signature": device.sign(body, ts),
                    "Content-Type": "application/json",
                },
            )
            assert ok.status_code == 200, ok.text
            assert ok.json()["compliant"] and "BEGIN CERTIFICATE" in ok.json()["certificate"]
            assert "PRIVATE KEY" not in ok.text
            replay = await client.post(
                "/api/v1/device/report",
                content=body,
                headers={
                    "X-Device-Id": str(device.id),
                    "X-Device-Timestamp": str(ts),
                    "X-Device-Signature": device.sign(body, ts),
                },
            )
            assert replay.status_code == 401
            assert replay.headers["content-type"].startswith("application/problem+json")
            listed = await client.get(
                f"/api/v1/tenants/{TENANT}/devices", headers={"Authorization": f"Bearer {viewer}"}
            )
            assert [x["name"] for x in listed.json()] == ["laptop-ana"]
            assert listed.json()[0]["compliant"] is True
            revoked = await client.post(
                f"/api/v1/tenants/{TENANT}/devices/{device.id}/revoke", headers=auth
            )
            assert revoked.json()["status"] == "revoked"
            gone = await client.delete(
                f"/api/v1/tenants/{TENANT}/devices/{device.id}", headers=auth
            )
            assert gone.status_code == 204
            missing = await client.get(
                f"/api/v1/tenants/{TENANT}/devices/{device.id}", headers=auth
            )
            assert missing.status_code == 404
            bad = await client.post(
                "/api/v1/device/enroll",
                json={"token": "sydev_nope", "name": "x", "public_key": device.public},
            )
            assert bad.status_code == 401
        off = create_app(build_services(w.db, StaticEngineDirectory({})))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=off), base_url="http://api"
        ) as client:
            r = await client.post(
                "/api/v1/device/enroll", json={"token": "x", "name": "y", "public_key": "z"}
            )
            assert r.status_code == 503
