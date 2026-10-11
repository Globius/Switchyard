"""HAProxy Data Plane API v3 client.

Talks to the Data Plane API that runs beside HAProxy on each SE (mTLS in production). Behaviour
learned from the real API (contract tests in ``tests/integration/test_dataplane_contract.py``):

- ``POST /v3/services/haproxy/configuration/raw?version=N`` replaces the whole configuration. The
  version must match (``409`` otherwise), the file is validated first (``400`` with HAProxy's
  message), the reload is asynchronous (``202`` + ``Reload-Id``, polled at ``/reloads/{id}``).
- The API rewrites the configuration (re-indents, adds ``# _version`` headers) but keeps comments,
  so the render fingerprint comment identifies what is running.
- Maps, general files and crt-lists live in separate storage endpoints. Uploads use
  ``skip_reload=true`` because the final configuration push performs the single reload.
- Uploaded file names have their dots rewritten, which is why object names contain none.
"""

import asyncio
import ssl
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from switchyard.adapters.haproxy import (
    CONFIG_PATH,
    ConfigRejected,
    EngineInfo,
    HaproxyError,
    PushResult,
    ReloadFailed,
    ServerAdminState,
    ServerStatus,
    Unreachable,
    VersionConflict,
    storage_kind,
)
from switchyard.render.haproxy import fingerprint_of

API = "/v3/services/haproxy"
_STORAGE = {"map": "maps", "general": "general", "crt_list": "ssl_crt_lists"}


def _server_status(row: dict[str, Any]) -> ServerStatus:
    stats = row["stats"]
    last_change = stats.get("lastchg")
    return ServerStatus(
        row["backend_name"],
        row["name"],
        str(stats.get("status", "")),
        check_status=str(stats["check_status"]) if stats.get("check_status") else None,
        last_check=str(stats["last_chk"]) if stats.get("last_chk") else None,
        last_change_s=int(last_change) if isinstance(last_change, int | float) else None,
    )


class DataPlaneEngine:
    """A :class:`~switchyard.adapters.haproxy.HaproxyEngine` backed by the Data Plane API."""

    def __init__(
        self,
        base_url: str,
        *,
        name: str,
        username: str | None = None,
        password: str | None = None,
        ca_file: Path | None = None,
        client_cert: tuple[Path, Path] | None = None,
        timeout_s: float = 10.0,
        reload_timeout_s: float = 30.0,
        poll_interval_s: float = 0.2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create the client.

        Args:
            base_url: ``https://host:port`` of the Data Plane API.
            name: Name used in logs and audit events.
            username: Basic-auth user (the API requires one in addition to mTLS).
            password: Basic-auth password; supplied by the secrets adapter, never stored in the DB.
            ca_file: CA that signed the API's server certificate.
            client_cert: ``(certificate, key)`` paths for the controller's client certificate.
            timeout_s: Per-request timeout.
            reload_timeout_s: How long to wait for a reload to finish.
            poll_interval_s: Delay between reload status polls.
            client: Pre-built HTTP client (tests).
        """
        self._name = name
        self._reload_timeout = reload_timeout_s
        self._poll = poll_interval_s
        if client is not None:
            self._http = client
            return
        verify: ssl.SSLContext | bool = True
        if ca_file is not None:
            verify = ssl.create_default_context(cafile=str(ca_file))
            if client_cert is not None:
                verify.load_cert_chain(str(client_cert[0]), str(client_cert[1]))
        auth = httpx.BasicAuth(username, password or "") if username else None
        self._http = httpx.AsyncClient(
            base_url=base_url, auth=auth, verify=verify, timeout=timeout_s
        )

    @property
    def name(self) -> str:
        """Engine name for logs."""
        return self._name

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()

    # -- transport ---------------------------------------------------------------------------

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise Unreachable(f"{self._name}: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text.strip()
        if isinstance(body, dict) and "message" in body:
            return str(body["message"])
        return response.text.strip()

    def _unexpected(self, what: str, response: httpx.Response) -> HaproxyError:
        return HaproxyError(
            f"{self._name}: {what}: HTTP {response.status_code}: {self._message(response)}"
        )

    async def _version(self) -> int:
        response = await self._send("GET", f"{API}/configuration/version")
        if response.status_code != 200:
            raise self._unexpected("reading configuration version", response)
        return int(response.text.strip())

    # -- files -------------------------------------------------------------------------------

    async def _upload(self, path: str, content: str) -> None:
        kind = storage_kind(path)
        name = path.rsplit("/", 1)[-1]
        if kind == "config":  # pragma: no cover - callers route the config separately
            raise ValueError("the configuration is not a storage file")
        segment = _STORAGE[kind]
        params = {"skip_reload": "true"}
        if kind == "general":
            replace = await self._send(
                "PUT",
                f"{API}/storage/{segment}/{name}",
                params=params,
                files={"file_upload": (name, content.encode(), "text/plain")},
            )
        else:
            replace = await self._send(
                "PUT",
                f"{API}/storage/{segment}/{name}",
                params=params,
                content=content.encode(),
                headers={"Content-Type": "text/plain"},
            )
        if replace.status_code in (200, 202, 204):
            return
        if replace.status_code != 404:
            raise self._unexpected(f"replacing {path}", replace)
        created = await self._send(
            "POST",
            f"{API}/storage/{segment}",
            params={"force_reload": "false"} if kind == "crt_list" else None,
            files={"file_upload": (name, content.encode(), "text/plain")},
        )
        if created.status_code not in (200, 201, 202):
            raise self._unexpected(f"creating {path}", created)

    async def _delete(self, path: str) -> None:
        kind = storage_kind(path)
        if kind == "config":  # pragma: no cover
            return
        response = await self._send(
            "DELETE",
            f"{API}/storage/{_STORAGE[kind]}/{path.rsplit('/', 1)[-1]}",
            params={"skip_reload": "true"},
        )
        if response.status_code not in (200, 202, 204, 404):
            raise self._unexpected(f"deleting {path}", response)

    async def _upload_all(self, files: Mapping[str, str]) -> None:
        for path in sorted(files):
            if path != CONFIG_PATH:
                await self._upload(path, files[path])

    # -- configuration -----------------------------------------------------------------------

    async def _post_config(self, config: str, *, validate_only: bool) -> httpx.Response:
        """POST the raw configuration, retrying once if the version moved under us."""
        for attempt in (1, 2):
            version = await self._version()
            params: dict[str, Any] = {"version": version}
            if validate_only:
                params["only_validate"] = "true"
            response = await self._send(
                "POST",
                f"{API}/configuration/raw",
                params=params,
                content=config.encode(),
                headers={"Content-Type": "text/plain"},
            )
            if response.status_code == 409 and attempt == 1:
                continue
            if response.status_code == 409:
                raise VersionConflict(f"{self._name}: {self._message(response)}")
            if response.status_code == 400:
                raise ConfigRejected(f"{self._name}: {self._message(response)}")
            if response.status_code not in (200, 201, 202):
                raise self._unexpected("pushing configuration", response)
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    async def validate(self, files: Mapping[str, str]) -> None:
        """Stage the supporting files and ask HAProxy to check the configuration."""
        await self._upload_all(files)
        await self._post_config(files[CONFIG_PATH], validate_only=True)

    async def push(
        self, files: Mapping[str, str], *, previous: Mapping[str, str] | None = None
    ) -> PushResult:
        """Upload supporting files, replace the configuration, wait for the reload."""
        await self._upload_all(files)
        before = await self.info()
        response = await self._post_config(files[CONFIG_PATH], validate_only=False)
        reload_id = response.headers.get("Reload-Id")
        if response.status_code == 202 and reload_id:
            await self._await_reload(reload_id)
            await self._await_new_worker(before, reload_id)
        for path in sorted(set(previous or {}) - set(files)):
            await self._delete(path)
        return PushResult(
            version=int(response.headers.get("Configuration-Version", "0")),
            fingerprint=await self.fingerprint(),
            reload_id=reload_id,
        )

    async def _await_reload(self, reload_id: str) -> None:
        deadline = time.monotonic() + self._reload_timeout
        while True:
            response = await self._send("GET", f"{API}/reloads/{reload_id}")
            if response.status_code == 200:
                body = response.json()
                if body.get("status") == "succeeded":
                    return
                if body.get("status") == "failed":
                    raise ReloadFailed(
                        f"{self._name}: reload {reload_id} failed: {body.get('response', '')}"
                    )
            if time.monotonic() > deadline:
                raise ReloadFailed(f"{self._name}: reload {reload_id} did not finish in time")
            await asyncio.sleep(self._poll)

    async def _await_new_worker(self, before: EngineInfo, reload_id: str) -> None:
        """Check the reload really started a new worker.

        The API reports a reload as succeeded once the reload command exited. If the new worker
        cannot start (for example it cannot bind a VIP), HAProxy keeps the old worker and the old
        configuration, and the pid does not change.
        """
        deadline = time.monotonic() + self._reload_timeout
        while True:
            now = await self.info()
            if now.pid != before.pid:
                return
            if time.monotonic() > deadline:
                raise ReloadFailed(
                    f"{self._name}: reload {reload_id} reported success but HAProxy is still "
                    f"running worker {now.pid} with the previous configuration; the new worker "
                    "failed to start (check the engine's HAProxy log)"
                )
            await asyncio.sleep(self._poll)

    async def info(self) -> EngineInfo:
        """Return the worker pid and uptime from the runtime ``show info``."""
        response = await self._send("GET", f"{API}/runtime/info")
        if response.status_code != 200:
            raise self._unexpected("reading runtime info", response)
        info = response.json().get("info", {})
        return EngineInfo(pid=int(info.get("pid", 0)), uptime_s=int(info.get("uptime", 0)))

    async def fingerprint(self) -> str | None:
        """Return the render fingerprint in the running configuration."""
        response = await self._send("GET", f"{API}/configuration/raw")
        if response.status_code != 200:
            raise self._unexpected("reading configuration", response)
        return fingerprint_of(response.text)

    # -- runtime -----------------------------------------------------------------------------

    async def server_statuses(self) -> list[ServerStatus]:
        """Return the live status of every backend server."""
        response = await self._send("GET", f"{API}/stats/native", params={"type": "server"})
        if response.status_code != 200:
            raise self._unexpected("reading statistics", response)
        rows = response.json().get("stats", [])
        return sorted(
            (_server_status(r) for r in rows),
            key=lambda s: (s.backend, s.server),
        )

    async def set_server_state(self, backend: str, server: str, state: ServerAdminState) -> None:
        """Set a server's admin state through the Runtime API (no reload)."""
        response = await self._send(
            "PUT",
            f"{API}/runtime/backends/{backend}/servers/{server}",
            json={"admin_state": state},
        )
        if response.status_code == 404:
            raise HaproxyError(f"{self._name}: server {backend}/{server} not found")
        if response.status_code != 200:
            raise self._unexpected(f"setting {backend}/{server} to {state}", response)
