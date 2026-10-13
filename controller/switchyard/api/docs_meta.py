# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Documentation that the OpenAPI document adds to every operation after the routes are collected.

Keeping it in one place has two benefits: the access rules can be read (and reviewed) as a table,
and ``tests/unit/test_api_docs.py`` can fail when a new route has no description, no access rule,
no parameter documentation or no request example. :func:`enrich` is called by
``switchyard.api.app`` when it builds the document.
"""

import re
from typing import Any

from switchyard.api import examples as ex

TAGS: list[dict[str, str]] = [
    {"name": "meta", "description": "Liveness and API conventions."},
    {
        "name": "auth",
        "description": "Sign-in (OIDC, SAML, break-glass), sessions, `GET /me` and agent "
        "enrollment. Browsers use the session cookie plus `X-CSRF-Token`; machines use bearer "
        "tokens.",
    },
    {"name": "tenants", "description": "Tenants: quotas and the approval requirement."},
    {
        "name": "networks",
        "description": "Networks the SE VMs attach to, including managed Proxmox SDN vnets.",
    },
    {
        "name": "vip-pools",
        "description": "Address ranges VIPs come from, with usage and allocations.",
    },
    {"name": "se-groups", "description": "Groups of service engines serving the same services."},
    {"name": "routing-profiles", "description": "VRRP and BGP settings for announcing VIPs."},
    {"name": "virtual-services", "description": "Listeners: VIP, ports, TLS, routes."},
    {
        "name": "grafana",
        "description": "Grafana embedded in the UI: one-time launch links, scoped to a tenant.",
    },
    {
        "name": "gslb",
        "description": "Global server load balancing on PowerDNS: zones and wide IPs.",
    },
    {
        "name": "alerts",
        "description": "Alert targets (webhook, chat, email), rules on events, metrics and certificate expiry, and what is firing.",
    },
    {
        "name": "devices",
        "description": "Device posture: enrollment tokens, enrolled devices and the signed "
        "reports of the device agent that turn into short-lived client certificates.",
    },
    {"name": "pools", "description": "Backend pools and members; runtime member state."},
    {"name": "health-monitors", "description": "Active health checks for pools."},
    {"name": "certificates", "description": "Certificates: import, issue, renew."},
    {"name": "policy-sets", "description": "Redirect rules and allow lists."},
    {
        "name": "change-sets",
        "description": "Every desired-state write goes through a change set: draft, review "
        "(diff), approve, apply.",
    },
    {"name": "revisions", "description": "Immutable snapshots after each applied change set."},
    {"name": "engines", "description": "Service engines (SE VMs): register, enable, retire."},
    {
        "name": "infrastructure",
        "description": "Proxmox actions: deploy SE VMs, apply SDN objects.",
    },
    {
        "name": "status",
        "description": "What the data plane reports: engine status, drift, virtual service "
        "health, the live stream and the tenant summary.",
    },
    {"name": "search", "description": "Find objects by name (command palette)."},
    {"name": "jobs", "description": "Background jobs: progress, logs, retry and cancel."},
    {"name": "audit", "description": "The append-only audit log and its action catalog."},
    {"name": "tokens", "description": "API tokens for machines."},
    {
        "name": "access",
        "description": "Role mappings, users, the permission matrix and session refresh.",
    },
    {"name": "identity providers", "description": "OIDC and SAML providers, with test sign-in."},
    {"name": "break-glass", "description": "The local emergency administrator and system tokens."},
    {"name": "system", "description": "Controller-wide settings (system administrators)."},
    {
        "name": "kubernetes",
        "description": "Kubernetes clusters whose Services and Gateways become virtual services.",
    },
    {
        "name": "quota requests",
        "description": "A tenant asks for a higher quota; a system administrator decides.",
    },
    {
        "name": "imports",
        "description": "Convert another vendor's configuration into a draft change set.",
    },
    {
        "name": "acme providers",
        "description": "Certificate authorities (ACME) and their accounts; system administrators "
        "write, everyone signed in reads.",
    },
    {
        "name": "dns providers",
        "description": "A tenant's DNS credentials for DNS-01 validation, and the catalogue of "
        "provider types with their forms.",
    },
]

PARAMS: dict[str, str] = {
    "authorization": "`Bearer <API token>`. Browsers send the session cookie instead.",
    "t": "Tenant name or id.",
    "change_set": "Read the state as this open change set would leave it (draft view).",
    "apply": "`true` applies the change immediately as a one-operation change set.",
    "if-match": "The object's version as a quoted integer (the `ETag` of a read), for example "
    '`"3"`.',
    "name": "Return only the object with exactly this name.",
    "cursor": "`next_cursor` of the previous page.",
    "limit": "Page size.",
    "object_id": "Object id.",
    "capture": "Id of a packet capture (`id` in the capture list).",
    "zone": "Zone name, for example `gslb.example.test` (a filter on the list route).",
    "cluster": "Kubernetes cluster id.",
    "request": "Quota request id.",
    "controller_member": "Controller cluster member id.",
    "cs": "Change set id.",
    "index": "Position of the operation in the change set, starting at 0.",
    "n": "Revision number.",
    "job_id": "Job id.",
    "engine": "Engine id.",
    "device": "Device id.",
    "service": "Virtual service id.",
    "group": "SE group id.",
    "pool": "Pool id.",
    "member": "Pool member id.",
    "pool_id": "VIP pool id.",
    "monitor_id": "Health monitor id.",
    "certificate": "Certificate id.",
    "token": "API token id.",
    "state": "Filter by state.",
    "kind": "Filter by job kind.",
    "se_group": "Filter by SE group id.",
    "protocol": "`http`, `tcp` or `udp`.",
    "actor": "Who acted, for example `user:alice`; end with `*` for a prefix.",
    "action": "Audit action (see `GET /audit-actions`); end with `*` for a prefix.",
    "object": "Exact object reference, for example `change_set:<id>`; on revisions, an object id.",
    "since": "Only events at or after this time (ISO 8601).",
    "until": "Only events before this time (ISO 8601).",
    "q": "Free text.",
    "status_class": "Only responses of this class: 2, 3, 4 or 5.",
    "window_s": "Seconds back from now.",
    "step_s": "Seconds between points.",
    "types": "Restrict to these object types (repeat the parameter).",
    "provider": "Identity provider id.",
    "idp": "Identity provider id.",
    "test_id": "Id returned by the test sign-in start.",
    "mapping": "Role mapping id.",
    "user": "User id.",
    "tenant": "Only this tenant (id or name).",
    "disabled": "`true` for disabled users only, `false` for enabled ones.",
    "code": "Authorization code from the provider.",
    "error": "Error code from the provider.",
    "return_to": "Local path to return to after sign-in.",
    "download": "`1` to send the document as a file download.",
    "follow": "Keep the stream open (`true`) or send one snapshot and close.",
    "interval": "Seconds between database polls.",
}

DESCRIPTIONS: dict[str, str] = {
    "GET /healthz": "Returns `{status: ok}` when the process is up. It does not check the "
    "database. Public.",
    "GET /jobs/{job_id}": "A job by id without naming the tenant (the id from an `Accepted` "
    "response). Prefer `GET /tenants/{t}/jobs/{job_id}`, which also returns stages and timing. "
    "The caller must have access to the job's tenant; `404` otherwise.",
    "GET /tenants": "Tenants the caller can access: all for a system administrator, otherwise "
    "those named by the caller's grants.",
    "POST /tenants": "Creates a tenant with its quotas and the approval requirement. System "
    "administrators only. `409` if the name exists.",
    "DELETE /tenants/{t}": "Deletes the tenant and everything in it (cascading, irreversible). "
    "System administrators only. Audited as `tenant.delete`.",
    "GET /tenants/{t}": "One tenant with quotas and `require_approval`.",
    "PATCH /tenants/{t}": "Changes quotas or `require_approval`. A tenant administrator may turn "
    "`require_approval` on; everything else, and turning it off, needs a system administrator "
    "(`403`). Audited as `tenant.update`.",
    "POST /tenants/{t}/change-sets": "Opens an empty draft. The draft is based on the current "
    "revision (`base_revision`). Several drafts can be open at once.",
    "GET /tenants/{t}/change-sets/{cs}": "A change set with its operations (`ops`), state, "
    "author, approver and the reject comment or error when there is one.",
    "POST /tenants/{t}/change-sets/{cs}/apply": "Queues the apply job: lock, render, validate, "
    "canary, rollout, commit. Returns `202` with the `job_id`; follow it with "
    "`GET /tenants/{t}/jobs/{job_id}`. Allowed from `draft` and `failed`. When the tenant has "
    "`require_approval`, a normal caller gets `403` and must use `request-approval`; only a "
    "system administrator who sends `break_glass_reason` may apply directly (audited as "
    "`change_set.break_glass_apply`). The UI should hide Apply when `tenant.require_approval` "
    "is true and the caller is not a system administrator. `409` if the state does not allow "
    "it, `422` with `errors[]` if the draft breaks a rule.",
    "POST /tenants/{t}/change-sets/{cs}/approve": "Approves a `pending_approval` change set and "
    "queues its apply. The approver must be a tenant administrator and must not be the author "
    "or the requester (`403`). `409` if it is not pending.",
    "POST /tenants/{t}/change-sets/{cs}/discard": "Throws a draft away (state `discarded`). "
    "Applied and applying change sets cannot be discarded (`409`).",
    "POST /tenants/{t}/change-sets/{cs}/ops": "Appends operations (create, update, delete of "
    "any object type) to a draft atomically; all are checked and none is kept if one is "
    "invalid (`422`). An operation may carry `expected_version`; a stale one answers `412`. "
    "Editing a `pending_approval` change set voids the request.",
    "DELETE /tenants/{t}/change-sets/{cs}/ops/{index}": "Removes one operation from a draft.",
    "POST /tenants/{t}/change-sets/{cs}/reject": "Sends a `pending_approval` change set back to "
    "`draft` with a required `comment`. Tenant administrators other than the author or "
    "requester.",
    "POST /tenants/{t}/change-sets/{cs}/request-approval": "Freezes a draft for review and sets "
    "`pending_approval`. Any later edit voids the request.",
    "DELETE /tenants/{t}/engines/{engine}": "Removes the engine *record* only: the VM is not "
    "touched and keeps running. Use it for hand-built engines or to forget a VM that is gone. "
    "To destroy a Proxmox VM as well, use `POST …/retire`.",
    "PUT /tenants/{t}/engines/{engine}/state": "Enables or disables an engine for rollouts "
    "(`state=enabled|disabled`). A disabled engine is skipped by applies and polling. Runtime "
    "action: it does not use a change set. Audited.",
    "GET /tenants/{t}/jobs": "The tenant's jobs, newest first, filtered by `state` and `kind`. "
    "Page with `cursor`.",
    "GET /tenants/{t}/jobs/{job_id}": "A job with its stages (name, state, timing, error), "
    "attempts, `retrying` and typed `result`. A reverted or rejected apply is a *finished* job "
    "with `ok: false`, not a failed one.",
    "POST /tenants/{t}/jobs/{job_id}/cancel": "Cancels a job that has not started. A cancelled "
    "apply marks its change set `failed`. `409` once the job is running or finished.",
    "GET /tenants/{t}/jobs/{job_id}/log": "The latest 500 log lines of the job.",
    "POST /tenants/{t}/jobs/{job_id}/retry": "Queues a `dead` job again. `409` if the job is "
    "not dead.",
    "PUT /tenants/{t}/pools/{pool}/members/{member}/state": "Sets a member to `enabled`, "
    "`disabled` or `drain` on every enabled engine of the SE groups that use the pool, through "
    "the HAProxy runtime API. This is a *runtime* action: no change set, takes effect at once, "
    "and desired state is unchanged (the next apply restores the configured state). The "
    "response lists the engines it reached (`applied`) and those it did not (`failed`, with the "
    "error), so partial failure can be shown. Audited as `pool_member.state`.",
    "GET /tenants/{t}/revisions/{n}": "A revision with its full desired-state snapshot. Diff "
    "two snapshots client-side to compare revisions.",
    "POST /tenants/{t}/revisions/{n}/rollback": "Opens a draft that restores revision `n`. It is "
    "an ordinary change set: review, approval and canary apply as usual. Returns `201` with the "
    "change set. Tenant administrators.",
    "GET /tenants/{t}/se-groups/{group}/engines": "Engines of one SE group (see also "
    "`GET /tenants/{t}/engines` for all).",
    "POST /tenants/{t}/se-groups/{group}/engines": "Registers a hand-built engine (Data Plane "
    "API URL, credentials reference, optional agent address). `409` if the name exists in the "
    "group. Engines created by `deploy` are registered automatically.",
    "GET /tenants/{t}/status/engines": "The last stored poll of every engine: reachability, "
    "HAProxy fingerprint, member check results, agent status (BGP, VRRP, IPVS) and drift. "
    "Polled about every 10 seconds; `observed_at` says how old it is.",
    "GET /tenants/{t}/tokens": "The tenant's API tokens (never the secret). Tenant administrators.",
    "POST /tenants/{t}/tokens": "Creates a token. The plaintext `token` is in this response "
    "only. A tenant administrator can issue up to their own role; a `system-admin` token needs "
    "a system administrator and has no tenant.",
    "DELETE /tenants/{t}/tokens/{token}": "Revokes a token; it stops working at once.",
}

DESCRIPTIONS.update(
    {
        "GET /identity-providers/{idp}": "One identity provider. Secrets are never returned: "
        "`client_secret_set` says whether one exists.",
        "DELETE /identity-providers/{idp}": "Deletes a provider and the role mappings that name it.",
        "GET /role-mappings": "All role mappings, system-wide and per tenant; narrow with `tenant`.",
        "POST /role-mappings": "Maps a provider group to a role, either system-wide "
        "(`system-admin`) or for a tenant. `409` if the provider, group and tenant already map.",
        "GET /role-mappings/{mapping}": "One role mapping with its provider, group, role and version.",
        "PATCH /role-mappings/{mapping}": "Changes the role or label of a mapping; `If-Match` on its "
        "version.",
        "DELETE /role-mappings/{mapping}": "Deletes a mapping. Users lose the grant at their next "
        "refresh (within a minute).",
        "GET /tenants/{t}/role-mappings": "Role mappings of this tenant. Tenant administrators see "
        "and manage `viewer` and `operator` mappings of their own tenant only.",
        "POST /tenants/{t}/role-mappings": "Maps a provider group to `viewer` or `operator` in this "
        "tenant (`403` for a stronger role).",
        "PATCH /tenants/{t}/role-mappings/{mapping}": "Changes a mapping of this tenant.",
        "DELETE /tenants/{t}/role-mappings/{mapping}": "Deletes a mapping of this tenant.",
        "GET /users": "Everyone who has signed in, with last sign-in and state. Filter by `tenant`, "
        "`disabled` and `q`; page with `cursor`.",
        "GET /users/{user}": "One user with their effective grants and the groups that gave them.",
        "DELETE /users/{user}": "Deletes the user record and ends their sessions; they can sign in "
        "again.",
        "POST /users/{user}/disable": "Refuses the user at sign-in and ends their sessions.",
        "POST /users/{user}/enable": "Lets a disabled user sign in again.",
        "POST /users/{user}/revoke-sessions": "Ends all of the user's sessions at once.",
        "GET /tenants/{t}/users": "People with a grant in this tenant, showing only this tenant's "
        "grants.",
        "GET /system/break-glass": "Whether the break-glass login is on, why, until when, and "
        "whether an administrator is enrolled.",
        "DELETE /system/break-glass/admin": "Removes the break-glass administrator and its secret.",
        "POST /system/break-glass/admin/confirm": "Finishes enrolment with a code from the "
        "authenticator; an unconfirmed administrator cannot sign in.",
        "GET /system/tokens": "System API tokens (never the secret).",
        "DELETE /system/tokens/{token}": "Revokes a system token; it stops working at once.",
    }
)

_RENDERED = (
    "The configuration fragment(s) this object produces for each SE group that uses it, "
    "with the fingerprint of the whole file. With `change_set` the draft's output. Pure "
    "rendering: nothing is sent to an engine."
)
for _slug in ("certificates", "health-monitors", "policy-sets", "pools", "se-groups"):
    DESCRIPTIONS[f"GET /tenants/{{t}}/{_slug}/{{object_id}}/rendered"] = _RENDERED
DESCRIPTIONS["GET /tenants/{t}/virtual-services/{object_id}/rendered"] = _RENDERED


#: ``(method, path regex) -> (minimum role, public)``; the first match wins.
ACCESS: list[tuple[str, str, str, bool]] = [
    ("GET", r"/healthz", "public", True),
    ("GET", r"/auth/(providers|login/\{provider\}|callback/\{provider\}|saml/.*)", "public", True),
    ("POST", r"/auth/(break-glass|callback/\{provider\})", "public", True),
    ("POST", r"/enroll", "public", True),
    ("POST", r"/device/(enroll|report)", "public", True),
    (
        "(POST|DELETE)",
        r"/tenants/\{t\}/(device-enrollments|devices(/\{device\}(/revoke)?)?)",
        "tenant-admin",
        False,
    ),
    ("POST", r"/auth/logout", "authenticated", False),
    ("GET", r"/me", "authenticated", False),
    ("(GET|POST)", r"/auth/refresh|/permissions", "authenticated", False),
    ("POST", r"/auth/refresh", "authenticated", False),
    (
        "GET",
        r"/(acme-providers(/\{provider\})?|acme-provider-presets|dns-provider-types|waf/crs-rules)",
        "authenticated",
        False,
    ),
    ("(POST|PATCH|DELETE)", r"/acme-providers(/.*)?", "system-admin", False),
    ("(POST|PATCH|DELETE)", r"/tenants/\{t\}/dns-providers(/.*)?", "tenant-admin", False),
    ("(POST|PATCH|DELETE)", r"/tenants/\{t\}/kubernetes-clusters(/.*)?", "tenant-admin", False),
    ("POST", r"/tenants/\{t\}/quota-requests(/.*)?", "tenant-admin", False),
    ("(GET|POST)", r"/quota-requests(/.*)?", "system-admin", False),
    (
        "(GET|POST|PATCH|PUT|DELETE)",
        r"/(identity-providers|role-mappings|system|users)(/.*)?",
        "system-admin",
        False,
    ),
    (
        "(GET|POST|PATCH|DELETE)",
        r"/tenants/\{t\}/(role-mappings|users)(/.*)?",
        "tenant-admin",
        False,
    ),
    ("GET", r"/schema/virtual-service", "authenticated", False),
    ("GET", r"/jobs/\{job_id\}", "authenticated", False),
    ("GET", r"/audit-actions", "authenticated", False),
    ("GET", r"/tenants", "authenticated", False),
    ("GET", r"/audit-events", "system-admin", False),
    ("POST", r"/tenants", "system-admin", False),
    ("DELETE", r"/tenants/\{t\}", "system-admin", False),
    ("PATCH", r"/tenants/\{t\}", "tenant-admin", False),
    (
        "(POST|PATCH|DELETE)",
        r"/tenants/\{t\}/(networks|vip-pools|se-groups|routing-profiles)(/\{object_id\})?",
        "tenant-admin",
        False,
    ),
    ("POST", r"/tenants/\{t\}/networks/apply-sdn", "tenant-admin", False),
    ("(GET|POST)", r"/tenants/\{t\}/tokens(/\{token\})?", "tenant-admin", False),
    ("DELETE", r"/tenants/\{t\}/tokens/\{token\}", "tenant-admin", False),
    ("POST", r"/tenants/\{t\}/vip-pools/\{pool_id\}/next-free", "viewer", False),
    ("(POST|DELETE)", r"/tenants/\{t\}/gslb-zones(/\{zone\})?", "system-admin", False),
    ("POST", r"/tenants/\{t\}/gslb-zones/\{zone\}/dnssec", "tenant-admin", False),
    (
        "(POST|PATCH|DELETE)",
        r"/tenants/\{t\}/gslb-zones/\{zone\}/wide-ips(/\{name\})?",
        "tenant-admin",
        False,
    ),
    ("POST", r"/tenants/\{t\}/gslb/sync", "tenant-admin", False),
    ("POST", r"/tenants/\{t\}/dns-records/sync", "tenant-admin", False),
    (
        "(GET|POST)",
        r"/tenants/\{t\}/(engines/\{engine\}/)?captures(/.*)?",
        "tenant-admin",
        False,
    ),
    ("POST", r"/tenants/\{t\}/grafana/launch", "viewer", False),
    (
        "(POST|PATCH|DELETE)",
        r"/tenants/\{t\}/alert-(targets|rules)(/\{name\}(/test)?)?",
        "tenant-admin",
        False,
    ),
    ("POST", r"/grafana/launch", "system-admin", False),
    ("POST", r"/tenants/\{t\}/health-monitors/\{monitor_id\}/test", "operator", False),
    ("(POST|PATCH|DELETE)", r"/tenants/\{t\}/[a-z-]+(/\{object_id\})?", "operator", False),
    (
        "POST",
        r"/tenants/\{t\}/certificates/(import|issue|\{certificate\}/renew)",
        "operator",
        False,
    ),
    (
        "(POST|PUT|DELETE)",
        r"/tenants/\{t\}/(se-groups/\{group\}/(deploy|engines)|engines/.*)",
        "tenant-admin",
        False,
    ),
    ("POST", r"/tenants/\{t\}/change-sets/\{cs\}/(approve|reject)", "tenant-admin", False),
    ("POST", r"/tenants/\{t\}/revisions/\{n\}/rollback", "tenant-admin", False),
    ("(POST|PUT|DELETE)", r"/tenants/\{t\}/.*", "operator", False),
    ("GET", r".*", "viewer", False),
]

_ROLE_TEXT = {
    "public": "Public: no credentials needed.",
    "authenticated": "Any authenticated caller.",
    "viewer": "Any role in the tenant (`viewer` or higher).",
    "operator": "`operator` or higher in the tenant.",
    "tenant-admin": "`tenant-admin` or higher in the tenant.",
    "system-admin": "`system-admin` only.",
}


def access_for(method: str, path: str) -> tuple[str, bool]:
    """The minimum role of an operation and whether it needs no credentials.

    Example:
        >>> access_for("GET", "/tenants/{t}/pools")
        ('viewer', False)
        >>> access_for("POST", "/tenants/{t}/se-groups")
        ('tenant-admin', False)
    """
    for m, pattern, role, public in ACCESS:
        if re.fullmatch(m, method.upper()) and re.fullmatch(pattern, path):
            return role, public
    raise KeyError(f"no access rule for {method} {path}")


_OBJECT_ROUTE = re.compile(r"/tenants/\{t\}/(?P<slug>[a-z-]+)(?P<one>/\{object_id\})?")


def _patch_example(slug: str) -> dict[str, Any] | None:
    examples: dict[str, dict[str, Any]] = {
        "networks": {"gateway": "192.0.2.254"},
        "vip-pools": {"ranges": [{"start": "192.0.2.100", "end": "192.0.2.220"}]},
        "se-groups": {"maxconn": 40000},
        "virtual-services": {"enabled": False},
        "pools": {"algorithm": "leastconn"},
        "health-monitors": {"interval_s": 10},
        "certificates": {"renew_policy": "manual"},
        "policy-sets": {
            "kind": "allow_list",
            "cidrs": ["198.51.100.0/24"],
        },
        "routing-profiles": {"kind": "vrrp", "advert_interval_s": 2},
    }
    return examples.get(slug)


REQUEST_EXAMPLES: dict[str, Any] = {
    "POST /tenants/{t}/engines/{engine}/captures": {
        "interface": "any",
        "protocol": "tcp",
        "host": "192.0.2.20",
        "port": 443,
        "duration_s": 10,
        "max_packets": 1000,
    },
    "POST /tenants/{t}/gslb-zones": {
        "name": "gslb.example.test",
        "nameservers": ["ns1.example.test"],
    },
    "POST /tenants/{t}/gslb-zones/{zone}/wide-ips": {
        "name": "shop",
        "algorithm": "failover",
        "members": [
            {"address": "192.0.2.10", "site": "pve1", "priority": 1},
            {"virtual_service": "shop", "site": "pve2", "priority": 2},
        ],
    },
    "PATCH /tenants/{t}/gslb-zones/{zone}/wide-ips/{name}": {"ttl": 60},
    "POST /tenants/{t}/alert-targets": {
        "name": "ops-chat",
        "kind": "chat",
        "config": {"url": "https://hooks.example.test/services/T000/B000/XXXX", "format": "slack"},
    },
    "PATCH /tenants/{t}/alert-targets/{name}": {"enabled": False},
    "POST /tenants/{t}/alert-rules": {
        "name": "certs-expiring",
        "kind": "cert_expiry",
        "spec": {"days": [30, 14, 7]},
        "targets": ["ops-chat"],
    },
    "PATCH /tenants/{t}/alert-rules/{name}": {"enabled": False},
    "POST /tenants/{t}/grafana/launch": {"landing": "/d/sy-tenant-services"},
    "POST /grafana/launch": {"landing": ""},
    "POST /auth/break-glass": {
        "username": "breakglass",
        "password": "example-password",
        "totp": "123456",
    },
    "POST /tenants/{t}/device-enrollments": {
        "policy_id": "0198b7c0-0000-7000-8000-000000000007",
        "label": "Ana's laptop",
    },
    "POST /device/enroll": {
        "token": "sydev_0123456789",
        "name": "laptop-ana",
        "public_key": "q0y0uUa3C5Qc4q2Ew2n7Fq0vXH1nDhI0zvY1m9eF8bA=",
        "owner": "ana@example.test",
    },
    "POST /device/report": {
        "facts": {
            "os": "windows",
            "os_version": "10.0.22631",
            "disk_encrypted": True,
            "firewall_enabled": True,
            "days_since_update": 6,
            "agent_version": "1.0.0",
        },
        "csr": "-----BEGIN CERTIFICATE REQUEST-----\nMIIB...\n-----END CERTIFICATE REQUEST-----\n",
    },
    "POST /enroll": {
        "token": "sy_enroll_0123456789",
        "csr": "-----BEGIN CERTIFICATE REQUEST-----\nMIIB...\n-----END CERTIFICATE REQUEST-----\n",
    },
    "POST /tenants": {"name": "acme", "quotas": {"virtual_service": 100}, "require_approval": True},
    "PATCH /tenants/{t}": {"require_approval": True},
    "POST /tenants/{t}/certificates/import": {
        "name": "web-example",
        "pem": "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----\n"
        "-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----\n",
    },
    "POST /tenants/{t}/certificates/import-ca": {
        "name": "partners",
        "pem": "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----\n",
        "crl_pem": "-----BEGIN X509 CRL-----\nMIIB...\n-----END X509 CRL-----\n",
    },
    "POST /tenants/{t}/certificates/{certificate}/replace": {
        "pkcs12_b64": "MIIJ...",
        "password": "export-password",
    },
    "POST /tenants/{t}/certificates/issue": {
        "name": "web-example",
        "source": "acme",
        "sans": ["www.example.test"],
        "acme_provider": "letsencrypt",
        "challenge": "dns-01",
        "dns_provider": "cloudflare-main",
        "renew_policy": "auto",
        "renew_before_days": 30,
    },
    "POST /tenants/{t}/kubernetes-clusters": {
        "name": "prod",
        "api_url": "https://k8s.example.test:6443",
        "token": "eyJhbGciOi...",
        "se_group": "edge",
        "vip_pool": "main",
        "mode": "nodeport",
    },
    "PATCH /tenants/{t}/kubernetes-clusters/{cluster}": {"mode": "pods"},
    "POST /tenants/{t}/quota-requests": {
        "resource": "virtual_service",
        "requested_limit": 50,
        "reason": "Two product teams move onto the platform in November.",
    },
    "POST /quota-requests/{request}/approve": {"granted_limit": 40, "note": "40 for now."},
    "POST /quota-requests/{request}/deny": {"note": "Delete unused services first."},
    "POST /system/backup-targets": {
        "name": "nightly",
        "passphrase_ref": "secret/backup/passphrase",
        "target": {
            "kind": "s3",
            "endpoint": "https://s3.example.test",
            "bucket": "sy-backups",
            "access_key_ref": "secret/backup/access-key",
            "secret_key_ref": "secret/backup/secret-key",
        },
        "keep": 14,
    },
    "POST /system/backups": {"target": "nightly", "reason": "pre-upgrade"},
    "POST /system/backups/verify": {
        "target": "nightly",
        "archive": "switchyard-0.1.0-20261013T040000Z.syb",
    },
    "POST /system/controller/members": {
        "name": "ctl-esxi",
        "address": "10.0.0.13",
        "platform": "esxi",
        "host": "esxi-01",
        "nofailover": True,
        "patroni_url": "http://10.0.0.13:8008",
    },
    "PATCH /system/controller/members/{controller_member}": {"host": "esxi-02"},
    "POST /tenants/{t}/imports/avi": {
        "export": {"VirtualService": [], "Pool": []},
        "se_group": "edge",
        "vip_pool": "main",
        "vip_map": {"vip-20": "192.0.2.20"},
        "prefix": "avi-",
        "dry_run": True,
    },
    "POST /tenants/{t}/certificates/issue/check": {
        "name": "web-example",
        "source": "acme",
        "sans": ["www.example.test", "*.example.test"],
        "challenge": "dns-01",
    },
    "POST /tenants/{t}/dns-providers": {
        "name": "cloudflare-main",
        "type": "cloudflare",
        "zones": ["example.test"],
        "config": {},
        "credentials": {"api_token": "example-token"},
    },
    "PATCH /tenants/{t}/dns-providers/{provider}": {"zones": ["example.test", "example.org"]},
    "POST /tenants/{t}/dns-providers/{provider}/test": {"zone": "example.test"},
    "POST /acme-providers": {
        "name": "letsencrypt",
        "preset": "letsencrypt",
        "email": "pki@example.test",
        "terms_agreed": True,
        "default": True,
    },
    "PATCH /acme-providers/{provider}": {"email": "certs@example.test", "default": True},
    "POST /tenants/{t}/change-sets": {"summary": "Add the shop service"},
    "POST /tenants/{t}/change-sets/{cs}/apply": {"break_glass_reason": None},
    "POST /tenants/{t}/change-sets/{cs}/reject": {"comment": "Please also cover www2."},
    "POST /tenants/{t}/change-sets/{cs}/ops": {
        "ops": [
            {
                "object_type": "pool",
                "object_id": ex.POOL,
                "action": "update",
                "body": {"algorithm": "leastconn"},
                "expected_version": 3,
            }
        ]
    },
    "PUT /tenants/{t}/pools/{pool}/members/{member}/state": {"state": "drain"},
    "POST /tenants/{t}/se-groups/{group}/deploy": {
        "count": 2,
        "nodes": ["pve1", "pve2"],
        "template_vmid": 9000,
        "mgmt_pool_id": ex.VIP_POOL,
        "pool": "edge",
        "credentials_ref": "kv/tenants/acme/engines/dataplane",
        "ssh_public_keys": "ssh-ed25519 AAAA... ops@example.test",
        "guest_kind": "qemu",
    },
    "POST /tenants/{t}/se-groups/{group}/engines": {
        "name": "edge-1",
        "dataplane_url": "https://198.51.100.21:5555",
        "credentials_ref": "kv/tenants/acme/engines/dataplane",
        "mgmt_ip": "198.51.100.21",
        "agent_url": "198.51.100.21:7443",
    },
    "POST /tenants/{t}/health-monitors/{monitor_id}/test": {
        "pool_id": ex.POOL,
        "member_id": ex.MEMBER,
    },
    "POST /identity-providers": {
        "protocol": "oidc",
        "name": "authentik",
        "issuer_url": "https://sso.example.test/application/o/switchyard",
        "client_id": "switchyard",
        "client_secret_ref": "kv/idp/authentik",
        "groups_claim": "groups",
    },
    "POST /role-mappings": {
        "provider": "authentik",
        "group_name": "switchyard-admins",
        "role": "system-admin",
    },
    "POST /tenants/{t}/role-mappings": {
        "provider": "authentik",
        "group_name": "acme-operators",
        "role": "operator",
    },
    "PATCH /identity-providers/{idp}": {"protocol": "oidc", "enabled": False},
    "POST /identity-providers/{idp}/test": {"return_to": "/admin/identity-providers"},
    "PATCH /role-mappings/{mapping}": {"role": "tenant-admin"},
    "PATCH /tenants/{t}/role-mappings/{mapping}": {"role": "viewer"},
    "POST /system/break-glass/admin/confirm": {"totp": "123456"},
    "PATCH /system/settings": {"allow_lxc_guests": True},
    "PUT /system/controller/pending": {
        "fqdn": "sy.example.test",
        "vip": "192.0.2.11",
        "certificate": "controller-new",
        "confirm_within_s": 120,
    },
    "PUT /system/break-glass": {"enabled": True, "ttl_minutes": 30, "reason": "SSO outage"},
    "POST /system/break-glass/admin": {"username": "breakglass"},
    "POST /system/tokens": {"name": "bootstrap", "ttl_days": 30},
    "POST /tenants/{t}/tokens": {"name": "ci", "role": "operator", "ttl_days": 90},
}


def request_example(method: str, path: str) -> Any | None:
    """The documented request body of an operation, if it has one."""
    key = f"{method.upper()} {path}"
    if key in REQUEST_EXAMPLES:
        return REQUEST_EXAMPLES[key]
    m = _OBJECT_ROUTE.fullmatch(path)
    if m and m["slug"] in ex.CREATE_BODIES:
        if method.upper() == "POST" and not m["one"]:
            return ex.CREATE_BODIES[m["slug"]]
        if method.upper() == "PATCH" and m["one"]:
            return _patch_example(m["slug"])
    return None


#: Operations that answer 503 when an optional backend is not configured.
UNAVAILABLE: dict[str, str] = {
    "POST /tenants/{t}/certificates/import": "No secrets backend (OpenBao) is configured.",
    "POST /tenants/{t}/certificates/import-ca": "No secrets backend (OpenBao) is configured.",
    "POST /enroll": "Agent enrollment is not configured.",
    "POST /device/enroll": "Device enrollment is not configured.",
    "POST /device/report": "Device enrollment is not configured.",
    "POST /tenants/{t}/device-enrollments": "Device enrollment is not configured.",
    "POST /tenants/{t}/engines/{engine}/enrollment-token": "Agent enrollment is not configured.",
}

_CONFLICT = "The resource is in a state that does not allow this, or the name is already taken."
_BAD_CURSOR = "`cursor` is not a value this endpoint returned."


def enrich(schema: dict[str, Any]) -> None:
    """Add tags, security schemes, access rules, parameter and body documentation.

    Mutates the generated OpenAPI document in place. Idempotent.
    """
    prefix = "/api/v1"
    schema["tags"] = TAGS
    _add_sse_catalog(schema)
    components = schema.setdefault("components", {})
    components["securitySchemes"] = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "description": "An API token (`sy_...`) from `POST /tenants/{t}/tokens`. For "
            "machines; not subject to CSRF.",
        },
        "sessionCookie": {
            "type": "apiKey",
            "in": "cookie",
            "name": "sy_session",
            "description": "Browser session from the login flow. Requests that change "
            "something must also send `X-CSRF-Token` with the value from `GET /me`.",
        },
        "csrfToken": {
            "type": "apiKey",
            "in": "header",
            "name": "X-CSRF-Token",
            "description": "Value of `csrf_token` from `GET /me`; required on writes made with "
            "the session cookie.",
        },
    }
    schema["security"] = [{"bearerAuth": []}, {"sessionCookie": [], "csrfToken": []}]
    for full_path, item in schema["paths"].items():
        path = full_path.removeprefix(prefix)
        for method, op in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            key = f"{method.upper()} {path}"
            if not op.get("description"):
                op["description"] = DESCRIPTIONS.get(key, "")
            role, public = access_for(method, path)
            op["x-required-role"] = role
            if public:
                op["security"] = []
            access = "**Access:** " + _ROLE_TEXT[role]
            if access not in op["description"]:
                op["description"] = (op["description"] + "\n\n" + access).strip()
            for param in op.get("parameters", []):
                if not param.get("description"):
                    param["description"] = PARAMS.get(param["name"].lower(), "")
            responses = op["responses"]
            problem_body = {"application/problem+json": {"schema": _problem(schema)}}
            if role != "public":
                responses.setdefault(
                    "401",
                    {
                        "description": "Missing, invalid or expired credentials.",
                        "content": problem_body,
                    },
                )
                if True:
                    responses.setdefault(
                        "403",
                        {
                            "description": "The caller's role does not allow this.",
                            "content": problem_body,
                        },
                    )
            if "{" in path.replace("{t}", "") or "{t}" in path:
                responses.setdefault(
                    "404",
                    {
                        "description": "The tenant, object or provider does not exist (or the "
                        "caller cannot see it).",
                        "content": problem_body,
                    },
                )
            if method != "get" and "409" not in responses and "/tenants/" in path:
                responses["409"] = {
                    "description": _CONFLICT,
                    "content": {"application/problem+json": {"schema": _problem(schema)}},
                }
            if key in UNAVAILABLE or path.startswith("/auth/"):
                responses.setdefault(
                    "503",
                    {
                        "description": UNAVAILABLE.get(
                            key, "Login is not configured on this controller."
                        ),
                        "content": problem_body,
                    },
                )
            if any(p["name"] == "cursor" for p in op.get("parameters", [])):
                responses.setdefault(
                    "400",
                    {
                        "description": _BAD_CURSOR,
                        "content": {"application/problem+json": {"schema": _problem(schema)}},
                    },
                )
                if "Page with" not in op["description"] and "cursor" not in DESCRIPTIONS.get(
                    key, ""
                ):
                    op["description"] += (
                        "\n\n**Pagination:** `limit` and `cursor`; pass `next_cursor` back as "
                        "`cursor` until it is null."
                    )
            body = op.get("requestBody")
            if body:
                content = body.get("content", {}).get("application/json")
                example = request_example(method, path)
                if content is not None and example is not None and "example" not in content:
                    content["example"] = example


def _add_sse_catalog(schema: dict[str, Any]) -> None:
    """Publish the stream's event payload models as components and list them on the operation."""
    from pydantic import TypeAdapter

    from switchyard.api.stream_models import EVENTS

    op = schema["paths"]["/api/v1/tenants/{t}/status/stream"]["get"]
    catalog: dict[str, Any] = {}
    for name, (model, is_array, when) in EVENTS.items():
        generated = TypeAdapter(model).json_schema(ref_template="#/components/schemas/{model}")
        for key, definition in generated.pop("$defs", {}).items():
            schema["components"]["schemas"].setdefault(key, definition)
        title = generated.get("title", model.__name__)
        schema["components"]["schemas"].setdefault(title, generated)
        ref = {"$ref": f"#/components/schemas/{title}"}
        catalog[name] = {
            "description": when,
            "data": {"type": "array", "items": ref} if is_array else ref,
        }
    op["x-sse-events"] = catalog


def _problem(schema: dict[str, Any]) -> dict[str, Any]:
    return {"$ref": "#/components/schemas/ProblemOut"}
