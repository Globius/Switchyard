# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A small client for the Switchyard HTTP API, used by ``syctl api ...``.

It speaks only the documented REST API with a bearer token, so it works against any controller the
user can reach and is subject to the same roles and tenant isolation as every other client.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

API_PREFIX = "/api/v1"
FINISHED = {"succeeded", "failed", "dead"}


class ApiClientError(Exception):
    """The API answered with a problem document (or could not be reached)."""

    def __init__(
        self, status: int, detail: str, errors: list[dict[str, str]] | None = None
    ) -> None:
        """Keep the HTTP status, the problem ``detail`` and any field ``errors``."""
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.errors = errors or []


@dataclass(frozen=True)
class Profile:
    """Where and how to reach a controller."""

    url: str
    token: str


def config_path() -> Path:
    """Location of the saved profile (``SYCTL_CONFIG`` overrides it)."""
    override = os.environ.get("SYCTL_CONFIG")
    if override:
        return Path(override)
    base = (
        os.environ.get("XDG_CONFIG_HOME")
        or os.environ.get("APPDATA")
        or str(Path.home() / ".config")
    )
    return Path(base) / "syctl" / "config.json"


def save_profile(profile: Profile) -> Path:
    """Write the profile readable by the owner only. The token is stored in clear text there."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": profile.url, "token": profile.token}))
    path.chmod(0o600)
    return path


def load_profile() -> Profile:
    """Load the profile from the environment (``SYCTL_URL``, ``SYCTL_TOKEN``) or the saved file."""
    url, token = os.environ.get("SYCTL_URL"), os.environ.get("SYCTL_TOKEN")
    if url and token:
        return Profile(url, token)
    path = config_path()
    if not path.exists():
        raise ApiClientError(0, "not logged in: run 'syctl api login --url ... --token ...'")
    data = json.loads(path.read_text())
    return Profile(data["url"], data["token"])


class ApiClient:
    """Calls the API and turns problem+json answers into :class:`ApiClientError`."""

    def __init__(
        self, profile: Profile, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Create a client; ``transport`` lets tests talk to an in-process app."""
        self._http = httpx.AsyncClient(
            base_url=profile.url.rstrip("/") + API_PREFIX,
            headers={"Authorization": f"Bearer {profile.token}"},
            transport=transport,
            timeout=30.0,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Send a request and return the decoded JSON body (``None`` for 204)."""
        try:
            response = await self._http.request(
                method, path, json=json_body, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            raise ApiClientError(0, f"cannot reach the controller: {exc}") from exc
        if response.status_code >= 400:
            try:
                problem = response.json()
            except ValueError:
                problem = {}
            raise ApiClientError(
                response.status_code,
                str(problem.get("detail") or problem.get("title") or response.text[:200]),
                problem.get("errors"),
            )
        return response.json() if response.content else None

    async def wait_job(self, job_id: str, *, timeout_s: float = 300.0, poll_s: float = 1.0) -> Any:
        """Poll a job until it finishes. Raises if it did not succeed or the wait times out."""
        deadline = time.monotonic() + timeout_s
        while True:
            job = await self.request("GET", f"/jobs/{job_id}")
            if job["state"] in FINISHED:
                if job["state"] != "succeeded":
                    raise ApiClientError(0, f"job {job_id} {job['state']}: {job.get('error')}")
                return job
            if time.monotonic() > deadline:
                raise ApiClientError(0, f"job {job_id} did not finish within {timeout_s:.0f}s")
            await asyncio.sleep(poll_s)

    async def close(self) -> None:
        """Close the connection."""
        await self._http.aclose()
