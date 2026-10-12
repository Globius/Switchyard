"""``syctl api ...`` against a real server (uvicorn) over HTTP, run in a worker thread."""

import asyncio
import json
import socket
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import uvicorn
from typer.testing import CliRunner

from switchyard.adapters.secrets import MemorySecrets
from switchyard.api.app import build_services, create_app
from switchyard.cli import app as cli
from switchyard.domain.workflow import Role
from switchyard.jobs.handlers import build_handlers
from switchyard.jobs.worker import Worker
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.certificates import CertificateService
from tests.pipeline_env import GROUP, TENANT, World, engine_record, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")


class Server:
    def __init__(self, w: World, url: str, token: str) -> None:
        self.w, self.url, self.token = w, url, token
        self.runner = CliRunner()

    async def syctl(self, *args: str) -> tuple[int, str]:
        """Run ``syctl api ...`` in a thread (it brings its own event loop) and return the output."""
        result = await asyncio.to_thread(self.runner.invoke, cli, ["api", *args])
        return result.exit_code, result.output

    async def drain_jobs(self) -> None:
        worker = Worker(
            self.w.db.admin, build_handlers(self.w.db, self.w.pipeline, None), worker_id="t"
        )
        while await worker.run_once():
            await asyncio.sleep(0)


@pytest_asyncio.fixture
async def server(
    pg_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[Server]:
    async with world(pg_url) as w:
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        secrets = MemorySecrets()
        services = build_services(
            w.db, directory, secrets, CertificateService(w.db, secrets, w.service)
        )
        token, _ = await services.tokens.create(
            principal(Role.SYSTEM_ADMIN, "root"), None, "cli", Role.SYSTEM_ADMIN
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = uvicorn.Config(
            create_app(services), host="127.0.0.1", port=port, log_level="warning"
        )
        http = uvicorn.Server(config)
        task = asyncio.create_task(http.serve())
        while not http.started:
            await asyncio.sleep(0.05)
        monkeypatch.setenv("SYCTL_CONFIG", str(tmp_path / "syctl.json"))
        monkeypatch.delenv("SYCTL_URL", raising=False)
        monkeypatch.delenv("SYCTL_TOKEN", raising=False)
        try:
            yield Server(w, f"http://127.0.0.1:{port}", token)
        finally:
            http.should_exit = True
            await task


async def test_login_whoami_list_apply_get_delete_status(server: Server, tmp_path: Path) -> None:
    code, out = await server.syctl("whoami")
    assert code == 1 and "not logged in" in out

    code, out = await server.syctl("login", "--url", server.url, "--token", "sy_wrong")
    assert code == 1 and "401" in out

    code, out = await server.syctl("login", "--url", server.url, "--token", server.token)
    assert code == 0 and "logged in as token:cli" in out
    saved = json.loads((tmp_path / "syctl.json").read_text())
    assert saved["token"] == server.token

    code, out = await server.syctl("whoami")
    assert code == 0 and "system administrator" in out and "acme: system-admin" in out

    code, out = await server.syctl("list", "pools", "-t", "acme")
    assert code == 0 and out.startswith("web\t")

    body = tmp_path / "pool.json"
    body.write_text(
        json.dumps(
            {
                "name": "api",
                "members": [{"id": str(uuid.uuid4()), "address": "10.20.0.12", "port": 8080}],
            }
        )
    )
    applying = asyncio.create_task(server.syctl("apply", "pools", "-f", str(body), "-t", "acme"))
    for _ in range(100):  # play the worker while the CLI waits for the job
        await server.drain_jobs()
        if applying.done():
            break
        await asyncio.sleep(0.1)
    code, out = await applying
    assert code == 0, out
    assert "change set" in out

    code, out = await server.syctl("get", "pools", "api", "-t", "acme")
    assert code == 0 and json.loads(out)["members"][0]["address"] == "10.20.0.12"
    code, out = await server.syctl("get", "pools", "nope", "-t", "acme")
    assert code == 1 and "no pools named nope" in out

    pool_id = json.loads((await server.syctl("get", "pools", "api", "-t", "acme"))[1])["id"]
    deleting = asyncio.create_task(server.syctl("delete", "pools", pool_id, "-t", "acme"))
    for _ in range(100):
        await server.drain_jobs()
        if deleting.done():
            break
        await asyncio.sleep(0.1)
    code, out = await deleting
    assert code == 0 and "deleted" in out
    assert "api\t" not in (await server.syctl("list", "pools", "-t", "acme"))[1]


async def test_errors_show_the_field_problems(server: Server, tmp_path: Path) -> None:
    await server.syctl("login", "--url", server.url, "--token", server.token)
    body = tmp_path / "bad.json"
    body.write_text(json.dumps({"name": "Bad Name!", "members": []}))
    code, out = await server.syctl("apply", "pools", "-f", str(body), "-t", "acme", "--no-wait")
    assert code == 1 and "422" in out
    code, out = await server.syctl("list", "pools", "-t", "nope")
    assert code == 1 and "404" in out


async def test_status_and_job_commands(server: Server) -> None:
    from switchyard.services.status import StatusService

    await StatusService(server.w.db, server.w.pipeline._directory).poll_all()
    await server.syctl("login", "--url", server.url, "--token", server.token)
    code, out = await server.syctl("status", "-t", "acme")
    assert code == 0 and "UNREACHABLE" not in out
    code, out = await server.syctl("job", "00000000-0000-0000-0000-000000000000")
    assert code == 1 and "404" in out
    assert TENANT


async def test_acme_dns_and_cert_commands(server: Server) -> None:
    await server.syctl("login", "--url", server.url, "--token", server.token)

    code, out = await server.syctl("acme", "presets")
    assert code == 0 and "letsencrypt" in out and "keyfactor" in out
    code, out = await server.syctl(
        "acme",
        "add",
        "private",
        "--directory-url",
        "https://ca.example.test/dir",
        "--terms-agreed",
        "--default",
    )
    assert code == 0, out
    code, out = await server.syctl("acme", "list")
    assert "private" in out and "default" in out and "untested" in out
    code, out = await server.syctl("acme", "add", "bad", "--preset", "zerossl")
    assert code == 1 and "terms_agreed" in out and "eab_kid" in out

    code, out = await server.syctl("dns", "types")
    assert code == 0 and "cloudflare" in out and "api_token	secret	required" in out
    code, out = await server.syctl(
        "dns",
        "add",
        "cf",
        "-t",
        "acme",
        "--type",
        "cloudflare",
        "--zone",
        "example.test",
        "--secret",
        "api_token=tok",
    )
    assert code == 0, out
    code, out = await server.syctl("dns", "add", "cf", "-t", "acme", "--type", "cloudflare")
    assert code == 1 and "credentials.api_token" in out
    code, out = await server.syctl("dns", "list", "-t", "acme")
    assert "cf	cloudflare	example.test" in out and "tok" not in out

    code, out = await server.syctl(
        "cert", "check", "web", "-t", "acme", "--san", "a.example.test", "--san", "b.other.test"
    )
    assert code == 1 and "sans[1]: no DNS credential covers this name" in out
    code, out = await server.syctl("cert", "check", "web", "-t", "acme", "--san", "a.example.test")
    assert code == 0 and out.strip() == "ok"
    code, out = await server.syctl("cert", "renewals", "-t", "acme")
    assert code == 0

    code, out = await server.syctl("dns", "delete", "cf", "-t", "acme")
    assert code == 0, out
    code, out = await server.syctl("acme", "delete", "private")
    assert code == 0, out
    code, out = await server.syctl("acme", "list")
    assert out.strip() == ""


async def test_import_avi_reports_then_creates_a_draft(server: Server, tmp_path: Path) -> None:
    from tests.avi_support import export

    await server.syctl("login", "--url", server.url, "--token", server.token)
    path = tmp_path / "avi.json"
    path.write_text(json.dumps(export()))
    code, out = await server.syctl(
        "import-avi", str(path), "-t", "acme", "--se-group", "edge", "--vip-pool", "main"
    )
    assert code == 0, out
    assert out.startswith("dry run: ") and "3 virtual service" in out
    assert "VIP vip-20: 10.60.3.20 -> 192.0.2." in out
    assert "manual   virtual_service:External-ParentSNI: WAF policy detect-only" in out
    code, out = await server.syctl(
        "import-avi",
        str(path),
        "-t",
        "acme",
        "--se-group",
        "edge",
        "--vip-pool",
        "main",
        "--commit",
    )
    assert code == 0 and "created a draft change set" in out and "change set " in out
    code, out = await server.syctl("import-avi", str(path), "-t", "acme", "--se-group", "ghost")
    assert code == 1 and "se_group" in out
