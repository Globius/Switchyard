"""Grafana administration for the embedded dashboards (ADR 0085).

The controller keeps one Grafana organization per tenant, with the tenant's own data sources (which
point back at the controller's scoped data source proxy) and the standard dashboards, and one
Grafana user per person and tenant who is a Viewer in that organization only. This module is the
only code that talks to Grafana's HTTP API. :class:`HttpGrafana` is the real client,
:class:`FakeGrafana` records what it was asked for.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import httpx

#: The organization every installation has; the unscoped (administrator) dashboards live there.
MAIN_ORG_ID = 1


class GrafanaError(Exception):
    """Grafana failed or answered something unexpected."""


@dataclass(frozen=True)
class DatasourceSpec:
    """One data source of a tenant organization.

    Attributes:
        uid: Stable id the dashboards refer to (``prometheus``, ``loki``).
        name: Display name.
        type: Grafana plugin id (``prometheus`` or ``loki``).
        url: Where Grafana sends queries: the controller's scoped proxy, never the backend itself.
        bearer: Sent as ``Authorization: Bearer <bearer>`` on every query; stored encrypted in
            Grafana and never returned by its API.
        default: Whether this is the organization's default data source.
    """

    uid: str
    name: str
    type: str
    url: str
    bearer: str
    default: bool = False


class GrafanaAdmin(Protocol):
    """What the controller needs from Grafana's administration API."""

    async def ensure_org(self, name: str) -> int:
        """The id of the organization called ``name``, created when absent."""
        ...

    async def ensure_user(self, login: str, name: str, email: str) -> int:
        """The id of the user ``login``, created (with an unusable password) when absent."""
        ...

    async def confine_user(self, org_id: int, login: str, role: str) -> None:
        """Make ``login`` a member of this organization only, with exactly ``role``."""
        ...

    async def ensure_datasources(self, org_id: int, specs: list[DatasourceSpec]) -> None:
        """Create or update the data sources of an organization."""
        ...

    async def ensure_dashboards(
        self, org_id: int, dashboards: list[dict[str, Any]], home_uid: str | None
    ) -> None:
        """Create or overwrite the dashboards of an organization and set its home dashboard."""
        ...


class HttpGrafana:
    """Grafana's HTTP API, authenticated as a server administrator over a private address."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        """Create the client.

        Args:
            base_url: Grafana's internal URL (``http://grafana:3000``).
            user: Server administrator login.
            password: Its password (``SWITCHYARD_GRAFANA_ADMIN_PASSWORD``).
            client: Injected HTTP client (tests).
            timeout: Per-request timeout in seconds.
        """
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._auth = httpx.BasicAuth(user, password)

    async def _call(
        self,
        method: str,
        path: str,
        *,
        org_id: int | None = None,
        json: Any = None,
        ok: tuple[int, ...] = (),
    ) -> httpx.Response:
        headers = {"X-Grafana-Org-Id": str(org_id)} if org_id is not None else {}
        try:
            response = await self._client.request(
                method, self._base + path, json=json, headers=headers, auth=self._auth
            )
        except httpx.HTTPError as exc:
            raise GrafanaError(f"{method} {path}: {exc}") from exc
        if response.status_code >= 400 and response.status_code not in ok:
            raise GrafanaError(
                f"{method} {path}: HTTP {response.status_code} {response.text[:200]}"
            )
        return response

    async def ensure_org(self, name: str) -> int:
        """Look the organization up by name, create it when it does not exist."""
        found = await self._call("GET", f"/api/orgs/name/{quote(name, safe='')}", ok=(404,))
        if found.status_code == 404:
            created = await self._call("POST", "/api/orgs", json={"name": name}, ok=(409,))
            if created.status_code == 409:  # another controller node was first
                found = await self._call("GET", f"/api/orgs/name/{quote(name, safe='')}")
                return int(found.json()["id"])
            return int(created.json()["orgId"])
        return int(found.json()["id"])

    async def ensure_user(self, login: str, name: str, email: str) -> int:
        """Look the user up, create it with a throwaway password when it does not exist."""
        import secrets

        found = await self._call(
            "GET", f"/api/users/lookup?loginOrEmail={quote(login, safe='')}", ok=(404,)
        )
        if found.status_code != 404:
            return int(found.json()["id"])
        created = await self._call(
            "POST",
            "/api/admin/users",
            json={
                "name": name,
                "email": email,
                "login": login,
                "password": secrets.token_urlsafe(48),
            },
            ok=(412,),
        )
        if created.status_code == 412:  # created meanwhile elsewhere
            again = await self._call(
                "GET", f"/api/users/lookup?loginOrEmail={quote(login, safe='')}"
            )
            return int(again.json()["id"])
        return int(created.json()["id"])

    async def confine_user(self, org_id: int, login: str, role: str) -> None:
        """Make the user a member of this organization only, with exactly ``role``.

        Grafana gives a new user an organization of their own (named after their email) when
        ``auto_assign_org`` is off, with the user as its Admin. Left alone that person could add a
        data source of their own that skips the controller, so every other membership is removed
        and such a personal organization is deleted.
        """
        found = await self._call("GET", f"/api/users/lookup?loginOrEmail={quote(login, safe='')}")
        user = found.json()
        user_id = int(user["id"])
        added = await self._call(
            "POST",
            f"/api/orgs/{org_id}/users",
            json={"loginOrEmail": login, "role": role},
            ok=(409,),
        )
        if added.status_code == 409:
            await self._call("PATCH", f"/api/orgs/{org_id}/users/{user_id}", json={"role": role})
        listed = await self._call("GET", f"/api/users/{user_id}/orgs")
        personal = {user.get("email"), login}
        for org in listed.json():
            other = int(org["orgId"])
            if other == org_id:
                continue
            if org.get("name") in personal:
                await self._call("DELETE", f"/api/orgs/{other}")
            else:
                await self._call("DELETE", f"/api/orgs/{other}/users/{user_id}")

    async def ensure_datasources(self, org_id: int, specs: list[DatasourceSpec]) -> None:
        """Create the data sources that are missing and rewrite the others."""
        listed = await self._call("GET", "/api/datasources", org_id=org_id)
        existing = {d["uid"] for d in listed.json()}
        for spec in specs:
            body = {
                "uid": spec.uid,
                "name": spec.name,
                "type": spec.type,
                "access": "proxy",
                "url": spec.url,
                "isDefault": spec.default,
                "jsonData": {"httpHeaderName1": "Authorization"},
                "secureJsonData": {"httpHeaderValue1": f"Bearer {spec.bearer}"},
            }
            if spec.uid in existing:
                await self._call(
                    "PUT", f"/api/datasources/uid/{spec.uid}", org_id=org_id, json=body
                )
            else:
                await self._call("POST", "/api/datasources", org_id=org_id, json=body)

    async def ensure_dashboards(
        self, org_id: int, dashboards: list[dict[str, Any]], home_uid: str | None
    ) -> None:
        """Import the dashboards (overwriting edits) and make one the organization's home."""
        for dashboard in dashboards:
            await self._call(
                "POST",
                "/api/dashboards/db",
                org_id=org_id,
                json={
                    "dashboard": {**dashboard, "id": None},
                    "overwrite": True,
                    "message": "Managed by Switchyard",
                },
            )
        if home_uid:
            await self._call(
                "PUT", "/api/org/preferences", org_id=org_id, json={"homeDashboardUID": home_uid}
            )


@dataclass
class FakeGrafana:
    """Records what the controller asked for; ids are assigned in order."""

    orgs: dict[str, int] = field(default_factory=dict)
    users: dict[str, int] = field(default_factory=dict)
    members: dict[int, dict[str, str]] = field(default_factory=dict)
    datasources: dict[int, list[DatasourceSpec]] = field(default_factory=dict)
    dashboards: dict[int, list[str]] = field(default_factory=dict)
    homes: dict[int, str | None] = field(default_factory=dict)
    calls: int = 0

    async def ensure_org(self, name: str) -> int:
        """Assign the next organization id (the main organization is 1)."""
        self.calls += 1
        return self.orgs.setdefault(name, len(self.orgs) + 2)

    async def ensure_user(self, login: str, name: str, email: str) -> int:
        """Assign the next user id."""
        self.calls += 1
        return self.users.setdefault(login, len(self.users) + 1)

    async def confine_user(self, org_id: int, login: str, role: str) -> None:
        """Record the membership and drop every other one."""
        self.calls += 1
        for members in self.members.values():
            members.pop(login, None)
        self.members.setdefault(org_id, {})[login] = role

    async def ensure_datasources(self, org_id: int, specs: list[DatasourceSpec]) -> None:
        """Record the data sources."""
        self.calls += 1
        self.datasources[org_id] = list(specs)

    async def ensure_dashboards(
        self, org_id: int, dashboards: list[dict[str, Any]], home_uid: str | None
    ) -> None:
        """Record the dashboard uids and the home dashboard."""
        self.calls += 1
        self.dashboards[org_id] = [str(d["uid"]) for d in dashboards]
        self.homes[org_id] = home_uid
