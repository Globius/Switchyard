"""Certificate import, issuance, renewal and the Cloudflare IP sync, through real change sets."""

import copy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from switchyard.adapters.cloudflare import FakeCloudflare, IpRanges
from switchyard.adapters.secrets import MemorySecrets
from switchyard.db.tenancy import admin_session
from switchyard.domain.models import AllowListPolicySet, CertSource
from switchyard.domain.workflow import Role
from switchyard.services.certificates import CertificateService
from switchyard.services.certs import CertificateError
from switchyard.services.errors import PermissionDeniedError
from tests.certs_support import make_pem, sign_csr
from tests.factories import base_data
from tests.pipeline_env import CERT_SECRETS, TENANT, World, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")

SOON = (datetime.now(UTC) + timedelta(days=10)).isoformat()


def due_data(**extra: Any) -> dict[str, Any]:
    d = copy.deepcopy(base_data())
    d["certificates"].append(
        {
            "id": "cert-origin",
            "tenant_id": "t-acme",
            "name": "origin-example-test",
            "source": "cloudflare_origin",
            "sans": ["example.test", "*.example.test"],
            "not_after": SOON,
            "secret_ref": "kv/tenants/acme/certs/origin-example-test/old",
            "renew_policy": "auto",
        }
    )
    d["se_groups"][0]["canary_seconds"] = 0
    d.update(extra)
    return d


def service(w: World, secrets: MemorySecrets, cf: FakeCloudflare) -> CertificateService:
    return CertificateService(w.db, secrets, w.service, cloudflare=cf)


def origin_ca() -> FakeCloudflare:
    return FakeCloudflare(origin_ca=lambda names, csr: sign_csr(csr))


async def run_latest_change_set(w: World) -> UUID:
    async with admin_session(w.db.admin) as s:
        cs = (
            await s.execute(text("SELECT id FROM change_set ORDER BY created_at DESC LIMIT 1"))
        ).scalar_one()
    report = await w.pipeline.run(TENANT, cs)
    assert report.ok, report.error
    return UUID(str(cs))


async def test_import_checks_the_bundle_and_stores_the_key_under_a_new_reference(
    pg_url: str,
) -> None:
    async with world(pg_url) as w:
        secrets = MemorySecrets(CERT_SECRETS)
        svc = service(w, secrets, FakeCloudflare())
        body = await svc.import_pem(principal(), TENANT, "shop", make_pem(("shop.example.test",)))
        assert body["source"] == "upload" and body["sans"] == ["shop.example.test"]
        assert body["secret_ref"].startswith("kv/tenants/acme/certs/shop/")
        assert "BEGIN PRIVATE KEY" in await secrets.get(body["secret_ref"])
        assert "PRIVATE KEY" not in str(body), "the key never leaves the secrets backend"
        with pytest.raises(CertificateError, match="does not match"):
            await svc.import_pem(principal(), TENANT, "bad", make_pem(other_key=True))
        with pytest.raises(PermissionDeniedError):
            await svc.import_pem(principal(Role.VIEWER), TENANT, "x", make_pem())


async def test_issue_from_cloudflare_origin_ca_creates_the_certificate(pg_url: str) -> None:
    async with world(pg_url) as w:
        secrets = MemorySecrets(CERT_SECRETS)
        svc = service(w, secrets, origin_ca())
        await svc.issue(TENANT, "fresh", CertSource.CLOUDFLARE_ORIGIN, ("fresh.example.test",))
        await run_latest_change_set(w)
        cert = next(c for c in (await w.snapshot()).certificates if c.name == "fresh")
        assert cert.source is CertSource.CLOUDFLARE_ORIGIN and cert.renew_policy.value == "auto"
        assert "BEGIN CERTIFICATE" in await secrets.get(cert.secret_ref)


async def test_unconfigured_issuers_and_manual_sources_are_refused(pg_url: str) -> None:
    async with world(pg_url) as w:
        svc = CertificateService(w.db, MemorySecrets(), w.service)
        with pytest.raises(CertificateError, match="ACME is not configured"):
            await svc.issue(TENANT, "x", CertSource.ACME, ("x.example.test",))
        with pytest.raises(CertificateError, match="cannot be issued automatically"):
            await svc.issue(TENANT, "x", CertSource.UPLOAD, ("x.example.test",))


async def test_renewal_replaces_only_certificates_that_are_due(pg_url: str) -> None:
    async with world(pg_url, due_data()) as w:
        secrets = MemorySecrets(CERT_SECRETS)
        svc = service(w, secrets, origin_ca())
        assert await svc.renew_due() == ["acme/origin-example-test"]
        await run_latest_change_set(w)
        cert = next(c for c in (await w.snapshot()).certificates if c.name == "origin-example-test")
        assert cert.secret_ref != "kv/tenants/acme/certs/origin-example-test/old"
        assert cert.not_after > datetime.now(UTC) + timedelta(days=365)
        assert cert.version == 2
        assert await svc.renew_due() == [], "nothing is due any more"


async def test_a_failed_renewal_is_audited_and_does_not_stop_the_others(pg_url: str) -> None:
    async with world(pg_url, due_data()) as w:
        cf = origin_ca()
        cf.fail_on.add("issue_origin_ca")
        svc = service(w, MemorySecrets(CERT_SECRETS), cf)
        assert await svc.renew_due() == []
        async with admin_session(w.db.admin) as s:
            count = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM audit_event WHERE action='certificate.renewal_failed'"
                    )
                )
            ).scalar_one()
        assert count == 1


async def test_tenants_that_require_approval_get_a_change_set_waiting_for_a_person(
    pg_url: str,
) -> None:
    async with world(pg_url, due_data()) as w:
        async with admin_session(w.db.admin) as s:
            await s.execute(text("UPDATE tenant SET require_approval = true"))
        svc = service(w, MemorySecrets(CERT_SECRETS), origin_ca())
        await svc.renew_due()
        async with admin_session(w.db.admin) as s:
            state = (
                await s.execute(
                    text("SELECT state FROM change_set ORDER BY created_at DESC LIMIT 1")
                )
            ).scalar_one()
        assert state == "pending_approval"


async def test_cloudflare_ip_list_is_synced_through_a_change_set(pg_url: str) -> None:
    policy = {
        "kind": "allow_list",
        "id": "ps-cf",
        "tenant_id": "t-acme",
        "name": "cloudflare",
        "cidrs": ["198.51.100.0/24"],
        "managed_by": "cloudflare",
    }
    manual = {
        "kind": "allow_list",
        "id": "ps-manual",
        "tenant_id": "t-acme",
        "name": "office",
        "cidrs": ["203.0.113.0/24"],
    }
    async with world(pg_url, due_data(policy_sets=[policy, manual])) as w:
        cf = FakeCloudflare(ranges=IpRanges(("192.0.2.0/24", "198.18.0.0/15"), ("2001:db8::/32",)))
        svc = service(w, MemorySecrets(CERT_SECRETS), cf)
        assert await svc.sync_cloudflare_ips() == ["acme/cloudflare"]
        await run_latest_change_set(w)
        lists = {
            p.name: sorted(str(c) for c in p.cidrs)
            for p in (await w.snapshot()).policy_sets
            if isinstance(p, AllowListPolicySet)
        }
        assert lists["cloudflare"] == ["192.0.2.0/24", "198.18.0.0/15", "2001:db8::/32"]
        assert lists["office"] == ["203.0.113.0/24"], "lists not marked as managed are left alone"
        assert await svc.sync_cloudflare_ips() == [], "unchanged ranges open no change set"


async def test_sync_needs_cloudflare(pg_url: str) -> None:
    async with world(pg_url) as w:
        with pytest.raises(CertificateError, match="not configured"):
            await CertificateService(w.db, MemorySecrets(), w.service).sync_cloudflare_ips()


async def test_api_import_issue_and_renew_endpoints_and_the_worker_jobs(pg_url: str) -> None:
    import httpx

    from switchyard.api.app import build_services, create_app
    from switchyard.jobs.handlers import build_handlers
    from switchyard.jobs.worker import Worker
    from switchyard.pipeline.engines import StaticEngineDirectory

    async with world(pg_url, due_data()) as w:
        secrets = MemorySecrets(CERT_SECRETS)
        certs = service(w, secrets, origin_ca())
        services = build_services(w.db, StaticEngineDirectory({}), secrets, certs=certs)
        token, _ = await services.tokens.create(
            principal(Role.SYSTEM_ADMIN, "root"), TENANT, "ci", Role.OPERATOR
        )
        headers = {"Authorization": f"Bearer {token}"}
        base = f"/api/v1/tenants/{TENANT}/certificates"
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            r = await client.post(
                f"{base}/import?apply=true",
                headers=headers,
                json={"name": "shop", "pem": make_pem(("shop.example.test",))},
            )
            assert r.status_code == 202, r.text
            bad = await client.post(
                f"{base}/import?apply=true",
                headers=headers,
                json={"name": "bad", "pem": make_pem(other_key=True)},
            )
            assert bad.status_code == 422 and "does not match" in bad.text
            issue = await client.post(
                f"{base}/issue",
                headers=headers,
                json={
                    "name": "api-made",
                    "source": "cloudflare_origin",
                    "sans": ["api.example.test"],
                },
            )
            assert issue.status_code == 202
            cert_id = next(
                c.id for c in (await w.snapshot()).certificates if c.name == "origin-example-test"
            )
            renew = await client.post(f"{base}/{cert_id}/renew", headers=headers)
            assert renew.status_code == 202
            missing = await client.post(f"{base}/{UUID(int=7)}/renew", headers=headers)
            assert missing.status_code == 404

        handlers = build_handlers(w.db, w.pipeline, None, certs=certs)
        worker = Worker(w.db.admin, handlers, worker_id="t")
        while await worker.run_once():
            pass
        names = {c.name for c in (await w.snapshot()).certificates}
        assert "shop" in names, "the import change set was queued and applied by the job"
        async with admin_session(w.db.admin) as s:
            states = (
                await s.execute(text("SELECT kind, state FROM job WHERE kind LIKE '%cert%'"))
            ).all()
        assert sorted(states) == [("issue_cert", "succeeded"), ("issue_cert", "succeeded")]
