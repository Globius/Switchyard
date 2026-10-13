# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Every route is called with a token of each role; the status must match the permission matrix.

``ROUTES`` classifies each route of the API as a matrix entry or as open to any signed-in caller.
A route missing from the table fails ``test_every_route_is_classified``, so a new endpoint cannot
ship without a permission decision (ADR 0030).
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from switchyard.adapters.secrets import MemorySecrets
from switchyard.api.app import build_services, create_app
from switchyard.domain import permissions
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.oidc import OidcClient
from switchyard.services.sessions import SessionService
from tests.integration.test_api import Api
from tests.pipeline_env import GROUP, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")

V1 = "/api/v1"
TENANT_NAME = "acme"
ANY = "any"  # open to every signed-in caller, no role needed
PUBLIC = "public"  # no credentials needed

READ = ("read", "tenant_data")
APP = ("write", "application")
INFRA = ("write", "infrastructure")
DRAFT = ("edit", "draft")

COLLECTIONS_APP = ["virtual-services", "pools", "health-monitors", "certificates", "policy-sets"]
COLLECTIONS_INFRA = ["networks", "vip-pools", "se-groups", "routing-profiles"]

ROUTES: dict[tuple[str, str], Any] = {
    ("GET", "/healthz"): PUBLIC,
    ("GET", "/discovery/prometheus"): ("manage", "tenant"),
    ("GET", "/schema/virtual-service"): ANY,
    ("GET", "/auth/providers"): PUBLIC,
    ("GET", "/auth/login/{provider}"): PUBLIC,
    ("GET", "/auth/callback/{provider}"): PUBLIC,
    ("POST", "/auth/callback/{provider}"): PUBLIC,
    ("GET", "/auth/saml/{provider}/metadata"): PUBLIC,
    ("POST", "/auth/break-glass"): PUBLIC,
    ("POST", "/auth/logout"): ANY,
    ("POST", "/auth/refresh"): ANY,
    ("GET", "/me"): ANY,
    ("GET", "/permissions"): ANY,
    ("POST", "/enroll"): PUBLIC,
    ("POST", "/device/enroll"): PUBLIC,
    ("POST", "/device/report"): PUBLIC,
    ("POST", "/tenants/{t}/device-enrollments"): ("manage", "device"),
    ("GET", "/tenants/{t}/devices"): ("read", "tenant_data"),
    ("GET", "/tenants/{t}/devices/{device}"): ("read", "tenant_data"),
    ("POST", "/tenants/{t}/devices/{device}/revoke"): ("manage", "device"),
    ("DELETE", "/tenants/{t}/devices/{device}"): ("manage", "device"),
    ("GET", "/jobs/{job_id}"): ANY,
    ("GET", "/tenants"): ANY,
    ("POST", "/tenants"): ("manage", "tenant"),
    ("GET", "/tenants/{t}"): READ,
    ("PATCH", "/tenants/{t}"): ("enable", "require_approval"),
    ("DELETE", "/tenants/{t}"): ("manage", "tenant"),
    ("GET", "/tenants/{t}/audit-events"): READ,
    ("GET", "/tenants/{t}/principals"): READ,
    ("GET", "/tenants/{t}/virtual-services/{service}/engines"): READ,
    ("GET", "/tenants/{t}/virtual-services/{service}/engines/{engine}/rendered"): READ,
    ("GET", "/tenants/{t}/engines/{engine}/running-config"): READ,
    ("POST", "/tenants/{t}/certificates/import-ca"): ("manage", "certificate"),
    ("POST", "/tenants/{t}/se-groups/{group}/reapply"): ("rollback", "revision"),
    ("POST", "/tenants/{t}/se-groups/{group}/rolls"): ("deploy", "se_group"),
    ("POST", "/tenants/{t}/rolls/{roll}"): ("deploy", "se_group"),
    ("GET", "/tenants/{t}/rolls"): READ,
    ("GET", "/tenants/{t}/sdn/status"): READ,
    ("GET", "/tenants/{t}/audit-events/export"): READ,
    ("GET", "/tenants/{t}/change-sets"): READ,
    ("POST", "/tenants/{t}/change-sets"): DRAFT,
    ("GET", "/tenants/{t}/change-sets/{cs}"): READ,
    ("POST", "/tenants/{t}/change-sets/{cs}/ops"): DRAFT,
    ("DELETE", "/tenants/{t}/change-sets/{cs}/ops/{index}"): DRAFT,
    ("GET", "/tenants/{t}/change-sets/{cs}/diff"): READ,
    ("POST", "/tenants/{t}/change-sets/{cs}/apply"): ("apply", "change_set"),
    ("POST", "/tenants/{t}/change-sets/{cs}/discard"): DRAFT,
    ("POST", "/tenants/{t}/change-sets/{cs}/request-approval"): ("request_approval", "change_set"),
    ("POST", "/tenants/{t}/change-sets/{cs}/approve"): ("approve", "change_set"),
    ("POST", "/tenants/{t}/change-sets/{cs}/reject"): ("reject", "change_set"),
    ("GET", "/tenants/{t}/revisions"): READ,
    ("GET", "/tenants/{t}/revisions/{n}"): READ,
    ("POST", "/tenants/{t}/revisions/{n}/rollback"): ("rollback", "revision"),
    ("PUT", "/tenants/{t}/pools/{pool}/members/{member}/state"): ("set_state", "member"),
    ("GET", "/tenants/{t}/se-groups/{group}/engines"): READ,
    ("GET", "/tenants/{t}/se-groups/{group}/placement"): READ,
    ("POST", "/tenants/{t}/se-groups/{group}/engines"): ("manage", "engine"),
    ("PUT", "/tenants/{t}/engines/{engine}/state"): ("manage", "engine"),
    ("DELETE", "/tenants/{t}/engines/{engine}"): ("manage", "engine"),
    ("POST", "/tenants/{t}/engines/{engine}/enrollment-token"): ("create", "enrollment_token"),
    ("POST", "/tenants/{t}/engines/{engine}/retire"): ("manage", "engine"),
    ("POST", "/tenants/{t}/se-groups/{group}/deploy"): ("deploy", "se_group"),
    ("POST", "/tenants/{t}/networks/apply-sdn"): ("apply", "sdn"),
    ("POST", "/tenants/{t}/certificates/import"): ("manage", "certificate"),
    ("POST", "/tenants/{t}/certificates/issue"): ("manage", "certificate"),
    ("POST", "/tenants/{t}/certificates/{certificate}/renew"): ("manage", "certificate"),
    ("POST", "/tenants/{t}/certificates/{certificate}/replace"): ("manage", "certificate"),
    ("GET", "/tenants/{t}/status/engines"): READ,
    ("GET", "/tenants/{t}/status/se-groups"): READ,
    ("GET", "/tenants/{t}/status/virtual-services"): READ,
    ("GET", "/tenants/{t}/status/stream"): READ,
    ("GET", "/tenants/{t}/jobs"): READ,
    ("GET", "/tenants/{t}/jobs/{job_id}"): READ,
    ("GET", "/tenants/{t}/jobs/{job_id}/log"): READ,
    ("POST", "/tenants/{t}/jobs/{job_id}/retry"): ("retry", "job_own"),
    ("POST", "/tenants/{t}/jobs/{job_id}/cancel"): ("cancel", "job_own"),
    ("GET", "/tenants/{t}/tokens"): ("manage", "token"),
    ("POST", "/tenants/{t}/tokens"): ("manage", "token"),
    ("DELETE", "/tenants/{t}/tokens/{token}"): ("manage", "token"),
    ("GET", "/tenants/{t}/role-mappings"): ("manage", "role_mapping"),
    ("POST", "/tenants/{t}/role-mappings"): ("manage", "role_mapping"),
    ("PATCH", "/tenants/{t}/role-mappings/{mapping}"): ("manage", "role_mapping"),
    ("DELETE", "/tenants/{t}/role-mappings/{mapping}"): ("manage", "role_mapping"),
    ("GET", "/tenants/{t}/users"): ("list", "tenant_user"),
    ("GET", "/identity-providers"): ("manage", "identity_provider"),
    ("POST", "/identity-providers"): ("manage", "identity_provider"),
    ("GET", "/identity-providers/{idp}"): ("manage", "identity_provider"),
    ("PATCH", "/identity-providers/{idp}"): ("manage", "identity_provider"),
    ("DELETE", "/identity-providers/{idp}"): ("manage", "identity_provider"),
    ("POST", "/identity-providers/{idp}/test"): ("manage", "identity_provider"),
    ("GET", "/identity-providers/{idp}/test/{test_id}"): ("manage", "identity_provider"),
    ("GET", "/system/break-glass"): ("manage", "break_glass"),
    ("PUT", "/system/break-glass"): ("manage", "break_glass"),
    ("POST", "/system/break-glass/admin"): ("manage", "break_glass"),
    ("POST", "/system/break-glass/admin/confirm"): ("manage", "break_glass"),
    ("DELETE", "/system/break-glass/admin"): ("manage", "break_glass"),
    ("GET", "/system/tokens"): ("manage", "system_token"),
    ("POST", "/system/tokens"): ("manage", "system_token"),
    ("DELETE", "/system/tokens/{token}"): ("manage", "system_token"),
    ("GET", "/role-mappings"): ("manage", "role_mapping_privileged"),
    ("POST", "/role-mappings"): ("manage", "role_mapping_privileged"),
    ("GET", "/role-mappings/{mapping}"): ("manage", "role_mapping_privileged"),
    ("PATCH", "/role-mappings/{mapping}"): ("manage", "role_mapping_privileged"),
    ("DELETE", "/role-mappings/{mapping}"): ("manage", "role_mapping_privileged"),
    ("GET", "/users"): ("manage", "user"),
    ("GET", "/users/{user}"): ("manage", "user"),
    ("DELETE", "/users/{user}"): ("manage", "user"),
    ("POST", "/users/{user}/disable"): ("manage", "user"),
    ("POST", "/users/{user}/enable"): ("manage", "user"),
    ("POST", "/users/{user}/revoke-sessions"): ("manage", "user"),
}
for _slug in COLLECTIONS_APP + COLLECTIONS_INFRA:
    _write = APP if _slug in COLLECTIONS_APP else INFRA
    ROUTES[("GET", f"/tenants/{{t}}/{_slug}")] = READ
    ROUTES[("POST", f"/tenants/{{t}}/{_slug}")] = _write
    ROUTES[("GET", f"/tenants/{{t}}/{_slug}/{{object_id}}")] = READ
    ROUTES[("PATCH", f"/tenants/{{t}}/{_slug}/{{object_id}}")] = _write
    ROUTES[("DELETE", f"/tenants/{{t}}/{_slug}/{{object_id}}")] = _write
    ROUTES[("GET", f"/tenants/{{t}}/{_slug}/{{object_id}}/rendered")] = READ
for _read in (
    ("GET", "/tenants/{t}/engines"),
    ("GET", "/tenants/{t}/engines/{engine}"),
    ("GET", "/tenants/{t}/engines/{engine}/metrics"),
    ("GET", "/tenants/{t}/alert-targets"),
    ("GET", "/tenants/{t}/gslb-zones"),
    ("GET", "/tenants/{t}/gslb-zones/{zone}/ds"),
    ("GET", "/tenants/{t}/wide-ips"),
    ("GET", "/tenants/{t}/gslb/status"),
    ("GET", "/tenants/{t}/dns-records"),
    ("GET", "/tenants/{t}/autoscale"),
    ("GET", "/tenants/{t}/alert-rules"),
    ("GET", "/tenants/{t}/alerts"),
    ("GET", "/tenants/{t}/logs"),
    ("POST", "/tenants/{t}/grafana/launch"),
    ("GET", "/tenants/{t}/metrics/virtual-services"),
    ("GET", "/tenants/{t}/search"),
    ("GET", "/tenants/{t}/summary"),
    ("GET", "/tenants/{t}/vip-pools/{pool_id}/allocations"),
    ("POST", "/tenants/{t}/vip-pools/{pool_id}/next-free"),
    ("GET", "/tenants/{t}/virtual-services/{object_id}/logs"),
    ("GET", "/tenants/{t}/virtual-services/{object_id}/metrics"),
):
    ROUTES[_read] = READ
ROUTES[("POST", "/tenants/{t}/health-monitors/{monitor_id}/test")] = APP
ROUTES[("POST", "/grafana/launch")] = ("manage", "tenant")
ROUTES[("GET", "/alert-catalog")] = ANY
ROUTES[("POST", "/tenants/{t}/gslb-zones")] = ("manage", "tenant")
ROUTES[("DELETE", "/tenants/{t}/gslb-zones/{zone}")] = ("manage", "tenant")
ROUTES[("POST", "/tenants/{t}/gslb-zones/{zone}/dnssec")] = INFRA
ROUTES[("POST", "/tenants/{t}/gslb-zones/{zone}/wide-ips")] = INFRA
ROUTES[("PATCH", "/tenants/{t}/gslb-zones/{zone}/wide-ips/{name}")] = INFRA
ROUTES[("DELETE", "/tenants/{t}/gslb-zones/{zone}/wide-ips/{name}")] = INFRA
ROUTES[("POST", "/tenants/{t}/gslb/sync")] = INFRA
ROUTES[("POST", "/tenants/{t}/dns-records/sync")] = INFRA
for _capture in (
    ("POST", "/tenants/{t}/engines/{engine}/captures"),
    ("GET", "/tenants/{t}/captures"),
    ("GET", "/tenants/{t}/captures/{capture}"),
    ("GET", "/tenants/{t}/captures/{capture}/pcap"),
):
    ROUTES[_capture] = ("manage", "engine")
for _alert in ("targets", "rules"):
    ROUTES[("POST", f"/tenants/{{t}}/alert-{_alert}")] = INFRA
    ROUTES[("PATCH", f"/tenants/{{t}}/alert-{_alert}/{{name}}")] = INFRA
    ROUTES[("DELETE", f"/tenants/{{t}}/alert-{_alert}/{{name}}")] = INFRA
ROUTES[("POST", "/tenants/{t}/alert-targets/{name}/test")] = INFRA
ROUTES[("GET", "/audit-actions")] = ANY
ROUTES[("GET", "/audit-events")] = ("manage", "tenant")
ROUTES[("GET", "/principals")] = ("manage", "tenant")
ROUTES[("GET", "/acme-provider-presets")] = ANY
ROUTES[("GET", "/waf/crs-rules")] = ANY
ROUTES[("GET", "/acme-providers")] = ANY
ROUTES[("GET", "/acme-providers/{provider}")] = ANY
ROUTES[("GET", "/dns-provider-types")] = ANY
for _method, _path in (
    ("POST", "/acme-providers"),
    ("PATCH", "/acme-providers/{provider}"),
    ("DELETE", "/acme-providers/{provider}"),
    ("POST", "/acme-providers/{provider}/test"),
):
    ROUTES[(_method, _path)] = ("manage", "acme_provider")
for _method, _path in (
    ("POST", "/tenants/{t}/dns-providers"),
    ("PATCH", "/tenants/{t}/dns-providers/{provider}"),
    ("DELETE", "/tenants/{t}/dns-providers/{provider}"),
    ("POST", "/tenants/{t}/dns-providers/{provider}/test"),
):
    ROUTES[(_method, _path)] = ("manage", "dns_provider")
for _path in (
    "/tenants/{t}/dns-providers",
    "/tenants/{t}/dns-providers/match",
    "/tenants/{t}/dns-providers/{provider}",
    "/tenants/{t}/certificate-renewals",
    "/tenants/{t}/certificates/{certificate}/renewals",
):
    ROUTES[("GET", _path)] = READ
ROUTES[("POST", "/tenants/{t}/certificates/issue/check")] = ("manage", "certificate")
ROUTES[("POST", "/tenants/{t}/imports/avi")] = DRAFT
for _method, _path in (
    ("POST", "/tenants/{t}/kubernetes-clusters"),
    ("PATCH", "/tenants/{t}/kubernetes-clusters/{cluster}"),
    ("DELETE", "/tenants/{t}/kubernetes-clusters/{cluster}"),
    ("POST", "/tenants/{t}/kubernetes-clusters/{cluster}/test"),
    ("POST", "/tenants/{t}/kubernetes-clusters/{cluster}/sync"),
):
    ROUTES[(_method, _path)] = ("manage", "kubernetes_cluster")
for _path in (
    "/tenants/{t}/kubernetes-clusters",
    "/tenants/{t}/kubernetes-clusters/{cluster}",
):
    ROUTES[("GET", _path)] = READ
ROUTES[("GET", "/system/controller")] = ("manage", "controller")
ROUTES[("GET", "/system/upgrade/check")] = ("manage", "controller")
ROUTES[("DELETE", "/system/bundles/{bundle}")] = ("manage", "controller")
ROUTES[("GET", "/system/bundles/{bundle}/file")] = ("manage", "controller")
ROUTES[("GET", "/system/bundles/{bundle}")] = ("manage", "controller")
ROUTES[("POST", "/system/bundles/fetch")] = ("manage", "controller")
ROUTES[("PUT", "/system/bundles")] = ("manage", "controller")
ROUTES[("GET", "/system/bundles")] = ("manage", "controller")
ROUTES[("POST", "/system/releases/check")] = ("manage", "controller")
ROUTES[("PUT", "/system/releases")] = ("manage", "controller")
ROUTES[("GET", "/system/releases")] = ("manage", "controller")
ROUTES[("POST", "/system/backups/verify")] = ("manage", "controller")
ROUTES[("POST", "/system/backups/restore")] = ("manage", "controller")
ROUTES[("POST", "/system/backups")] = ("manage", "controller")
ROUTES[("GET", "/system/backups")] = ("manage", "controller")
ROUTES[("POST", "/system/backup-targets/{name}/test")] = ("manage", "controller")
ROUTES[("DELETE", "/system/backup-targets/{name}")] = ("manage", "controller")
ROUTES[("POST", "/system/backup-targets")] = ("manage", "controller")
ROUTES[("GET", "/system/backup-targets")] = ("manage", "controller")
for _method, _path in (
    ("GET", "/system/controller/members"),
    ("POST", "/system/controller/members"),
    ("PATCH", "/system/controller/members/{controller_member}"),
    ("DELETE", "/system/controller/members/{controller_member}"),
):
    ROUTES[(_method, _path)] = ("manage", "controller")
ROUTES[("GET", "/tenants/{t}/quota-requests")] = READ
ROUTES[("POST", "/tenants/{t}/quota-requests")] = ("request", "quota_request")
ROUTES[("POST", "/tenants/{t}/quota-requests/{request}/withdraw")] = ("request", "quota_request")
ROUTES[("GET", "/quota-requests")] = ("decide", "quota_request")
ROUTES[("POST", "/quota-requests/{request}/approve")] = ("decide", "quota_request")
ROUTES[("POST", "/quota-requests/{request}/deny")] = ("decide", "quota_request")
ROUTES[("PUT", "/system/controller/pending")] = ("manage", "controller")
ROUTES[("POST", "/system/controller/confirm")] = ("manage", "controller")
ROUTES[("POST", "/system/controller/revert")] = ("manage", "controller")
ROUTES[("GET", "/system/settings")] = ("manage", "tenant")
ROUTES[("PATCH", "/system/settings")] = ("manage", "tenant")
ROUTES[("GET", "/audit-events/export")] = ("manage", "tenant")
ROUTES.pop(("GET", "/tenants/{t}/networks/{object_id}/rendered"), None)
ROUTES.pop(("GET", "/tenants/{t}/vip-pools/{object_id}/rendered"), None)
ROUTES.pop(("GET", "/tenants/{t}/routing-profiles/{object_id}/rendered"), None)

# valid request bodies, so the role check (not body validation) decides the status
NOW = "2038-01-01T00:00:00+00:00"
BODIES: dict[tuple[str, str], Any] = {
    ("POST", "/tenants/{t}/device-enrollments"): {"policy_id": "{uuid}"},
    ("POST", "/tenants/{t}/engines/{engine}/captures"): {"interface": "any", "duration_s": 5},
    ("POST", "/tenants/{t}/health-monitors/{monitor_id}/test"): {
        "pool_id": "{uuid}",
        "member_id": "{uuid}",
    },
    ("POST", "/tenants/{t}/alert-targets"): {
        "name": "ops",
        "kind": "webhook",
        "config": {"url": "https://hooks.example.test/in"},
    },
    ("PATCH", "/tenants/{t}/alert-targets/{name}"): {"enabled": False},
    ("POST", "/tenants/{t}/alert-rules"): {
        "name": "expiry",
        "kind": "cert_expiry",
        "spec": {},
        "targets": ["ops"],
    },
    ("PATCH", "/tenants/{t}/alert-rules/{name}"): {"enabled": False},
    ("POST", "/tenants/{t}/gslb-zones"): {
        "name": "gslb.example.test",
        "nameservers": ["ns1.example.test"],
    },
    ("POST", "/tenants/{t}/gslb-zones/{zone}/wide-ips"): {
        "name": "shop",
        "members": [{"address": "192.0.2.10"}],
    },
    ("PATCH", "/tenants/{t}/gslb-zones/{zone}/wide-ips/{name}"): {"ttl": 60},
    ("POST", "/tenants"): {"name": "newco"},
    ("PATCH", "/tenants/{t}"): {"require_approval": True},
    ("POST", "/tenants/{t}/change-sets"): {},
    ("POST", "/tenants/{t}/change-sets/{cs}/ops"): {
        "ops": [
            {
                "object_type": "pool",
                "object_id": "{uuid}",
                "action": "delete",
                "expected_version": 1,
            }
        ]
    },
    ("POST", "/tenants/{t}/change-sets/{cs}/apply"): {},
    ("POST", "/tenants/{t}/change-sets/{cs}/reject"): {"comment": "no"},
    ("PUT", "/tenants/{t}/pools/{pool}/members/{member}/state"): {"state": "drain"},
    ("POST", "/tenants/{t}/se-groups/{group}/engines"): {
        "name": "se-x",
        "dataplane_url": "https://se-x.invalid:5556",
    },
    ("POST", "/tenants/{t}/se-groups/{group}/deploy"): {
        "count": 1,
        "nodes": ["pve1"],
        "template_vmid": 9000,
        "mgmt_pool_id": "{uuid}",
    },
    ("POST", "/tenants/{t}/certificates/import"): {"name": "c1", "pem": "x"},
    ("POST", "/tenants/{t}/certificates/import-ca"): {"name": "ca1", "pem": "x"},
    ("PATCH", "/system/settings"): {"allow_lxc_guests": False},
    ("POST", "/system/backup-targets"): {
        "name": "bk",
        "passphrase_ref": "r/p",
        "target": {"kind": "local"},
    },
    ("POST", "/system/backups"): {"target": "bk"},
    ("POST", "/system/bundles/fetch"): {"version": "9.9.9"},
    ("POST", "/tenants/{t}/se-groups/{group}/rolls"): {"template_vmid": 9770},
    ("POST", "/tenants/{t}/rolls/{roll}"): {"action": "pause"},
    ("PUT", "/system/releases"): {"enabled": False},
    ("POST", "/system/backups/verify"): {"target": "bk", "archive": "x.syb"},
    ("POST", "/system/backups/restore"): {"target": "bk", "archive": "x.syb", "dry_run": True},
    ("POST", "/system/controller/members"): {
        "name": "ctl-x",
        "address": "10.0.0.13",
        "platform": "esxi",
    },
    ("PATCH", "/system/controller/members/{controller_member}"): {},
    ("POST", "/tenants/{t}/quota-requests"): {
        "resource": "pool",
        "requested_limit": 5,
        "reason": "growth",
    },
    ("POST", "/quota-requests/{request}/approve"): {},
    ("POST", "/quota-requests/{request}/deny"): {"note": "no"},
    ("PUT", "/system/controller/pending"): {"fqdn": "sy.example.test"},
    ("POST", "/tenants/{t}/imports/avi"): {"export": {}, "se_group": "edge"},
    ("POST", "/tenants/{t}/kubernetes-clusters"): {
        "name": "x",
        "api_url": "https://k8s.example.test:6443",
        "token": "t",
        "se_group": "edge",
        "vip_pool": "main",
    },
    ("PATCH", "/tenants/{t}/kubernetes-clusters/{cluster}"): {},
    ("POST", "/acme-providers"): {"name": "x", "preset": "letsencrypt", "terms_agreed": True},
    ("PATCH", "/acme-providers/{provider}"): {},
    ("POST", "/tenants/{t}/dns-providers"): {
        "name": "x",
        "type": "cloudflare",
        "credentials": {"api_token": "t"},
    },
    ("PATCH", "/tenants/{t}/dns-providers/{provider}"): {},
    ("POST", "/tenants/{t}/dns-providers/{provider}/test"): {},
    ("POST", "/tenants/{t}/certificates/issue/check"): {
        "name": "c2",
        "source": "acme",
        "sans": ["a.example.test"],
    },
    ("POST", "/tenants/{t}/certificates/{certificate}/replace"): {"pem": "x"},
    ("POST", "/tenants/{t}/certificates/issue"): {
        "name": "c2",
        "source": "acme",
        "sans": ["a.example.test"],
    },
    ("POST", "/tenants/{t}/tokens"): {"name": "tk", "role": "viewer"},
    ("POST", "/identity-providers"): {
        "protocol": "oidc",
        "name": "idp-x",
        "issuer_url": "https://sso.example.test",
        "client_id": "c",
    },
    ("PATCH", "/identity-providers/{idp}"): {"protocol": "oidc"},
    ("PUT", "/system/break-glass"): {"enabled": False},
    ("POST", "/system/break-glass/admin/confirm"): {"totp": "000000"},
    ("POST", "/system/tokens"): {"name": "sys-x"},
    ("POST", "/tenants/{t}/role-mappings"): {
        "provider": "nobody",
        "group_name": "g",
        "role": "viewer",
    },
    ("PATCH", "/tenants/{t}/role-mappings/{mapping}"): {"label": "x"},
    ("POST", "/role-mappings"): {"provider": "nobody", "group_name": "g", "role": "system-admin"},
    ("PATCH", "/role-mappings/{mapping}"): {"label": "x"},
    ("POST", "/tenants/{t}/networks"): {"name": "n1", "cidr": "198.51.100.0/24"},
    ("POST", "/tenants/{t}/vip-pools"): {
        "name": "v1",
        "network_id": "{uuid}",
        "ranges": [{"start": "198.51.100.10", "end": "198.51.100.20"}],
    },
    ("POST", "/tenants/{t}/se-groups"): {
        "name": "g1",
        "ha_mode": "vrrp",
        "networks": [{"network_id": "{uuid}", "role": "vip"}],
    },
    ("POST", "/tenants/{t}/routing-profiles"): {
        "kind": "vrrp",
        "name": "r1",
        "vrid": 1,
        "interface": "eth0",
    },
    ("POST", "/tenants/{t}/virtual-services"): {
        "name": "vs1",
        "se_group_id": "{uuid}",
        "protocol": "tcp",
        "vip": "192.0.2.30",
        "ports": [{"port": 9000}],
    },
    ("POST", "/tenants/{t}/pools"): {
        "name": "p1",
        "members": [{"id": "{uuid}", "address": "10.20.0.9", "port": 80}],
    },
    ("POST", "/tenants/{t}/health-monitors"): {"name": "m1", "type": "tcp"},
    ("POST", "/tenants/{t}/certificates"): {
        "name": "c3",
        "source": "upload",
        "sans": ["a.example.test"],
        "not_after": NOW,
        "secret_ref": "kv/tenants/acme/certs/c3",
    },
    ("POST", "/tenants/{t}/policy-sets"): {
        "kind": "allow_list",
        "name": "al",
        "cidrs": ["192.0.2.0/24"],
    },
}
PATCH_BODY = {"name": "renamed"}
PATCH_BODIES: dict[str, Any] = {
    "policy-sets": {"kind": "allow_list", "name": "renamed"},
    "routing-profiles": {"kind": "vrrp", "name": "renamed"},
}
# paths that are valid enough to reach the role check but do not need a real object
SKIP_DENIED_BODY_CHECK: set[tuple[str, str]] = set()


def fill(path: str) -> str:
    """Replace the template parameters with a tenant name and fresh ids."""
    out = path.replace("{t}", TENANT_NAME)
    for key in (
        "cs",
        "engine",
        "device",
        "object_id",
        "job_id",
        "mapping",
        "user",
        "token",
        "certificate",
        "idp",
        "cluster",
        "controller_member",
        "roll",
        "bundle",
        "request",
        "test_id",
        "capture",
    ):
        out = out.replace("{" + key + "}", str(uuid.uuid4()))
    for key in ("pool", "member", "group", "monitor_id", "pool_id"):
        out = out.replace("{" + key + "}", str(uuid.uuid4()))
    if "/acme-providers/" in out or "/dns-providers/" in out:
        out = out.replace("{provider}", str(uuid.uuid4()))
    return out.replace("{index}", "0").replace("{n}", "1").replace("{provider}", "nobody")


def query_for(key: tuple[str, str]) -> str:
    """Query string that lets a collection write reach the role check."""
    method, template = key
    if (
        method in {"POST", "PATCH", "DELETE"}
        and template.split("/")[3:4]
        and (template.count("/") <= 4 or "{object_id}" in template)
    ):
        slug = template.split("/")[3]
        if slug in COLLECTIONS_APP + COLLECTIONS_INFRA:
            return "?apply=true"
    if key == ("PUT", "/tenants/{t}/engines/{engine}/state"):
        return "?state=disabled"
    return ""


def body_for(key: tuple[str, str]) -> Any:
    """The JSON body for a route, with ``{uuid}`` placeholders replaced."""
    raw = BODIES.get(key)
    if raw is None and key[0] == "PATCH" and "{object_id}" in key[1]:
        raw = PATCH_BODIES.get(key[1].split("/")[3], PATCH_BODY)
    if raw is None:
        return None
    text = repr(raw)
    while "{uuid}" in text:
        text = text.replace("{uuid}", str(uuid.uuid4()), 1)
    return eval(text)  # noqa: S307 - our own literal


@pytest_asyncio.fixture
async def api(pg_url: str) -> AsyncIterator[Api]:
    """The test world with login configured, so identity and break-glass routes are reachable."""
    async with world(pg_url) as w:
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        sessions = SessionService(
            w.db, OidcClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        )
        services = build_services(w.db, directory, MemorySecrets(), sessions=sessions)
        app = create_app(services)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as client:
            yield Api(w, services, client)


def test_every_route_is_classified() -> None:
    spec = create_app(None).openapi()  # type: ignore[arg-type]
    seen = {
        (method.upper(), path.removeprefix(V1))
        for path, methods in spec["paths"].items()
        for method in methods
    }
    unclassified = seen - set(ROUTES)
    assert not unclassified, f"decide who may call these routes: {sorted(unclassified)}"
    stale = set(ROUTES) - seen
    assert not stale, f"routes that no longer exist: {sorted(stale)}"
    for entry in ROUTES.values():
        if isinstance(entry, tuple):
            permissions.min_role(*entry)  # must exist in the matrix


#: Routes that make tokens: a token cannot create tokens, whatever its role.
TOKEN_MAKERS = {("POST", "/tenants/{t}/tokens"), ("POST", "/system/tokens")}
#: Server-sent events never end, so the stream is classified but not requested here; it uses the
#: same ``Who`` dependency and read check as the other status routes.
NOT_REQUESTED = {("GET", "/tenants/{t}/status/stream")}
#: Routes whose backing service is not wired in this test application (503 before any role check).
NOT_CONFIGURED = {
    ("POST", "/tenants/{t}/device-enrollments"),
    ("GET", "/tenants/{t}/devices"),
    ("GET", "/tenants/{t}/devices/{device}"),
    ("POST", "/tenants/{t}/devices/{device}/revoke"),
    ("DELETE", "/tenants/{t}/devices/{device}"),
    ("POST", "/tenants/{t}/engines/{engine}/enrollment-token"),
    ("POST", "/tenants/{t}/certificates/import"),
    ("POST", "/tenants/{t}/certificates/import-ca"),
    ("POST", "/tenants/{t}/certificates/issue"),
    ("POST", "/tenants/{t}/certificates/{certificate}/renew"),
    ("POST", "/tenants/{t}/certificates/{certificate}/replace"),
}
#: Routes that change the tenant itself go last, so they cannot affect the requests after them
#: (turning approval on makes later writes need a reviewer).
LAST = {("PATCH", "/tenants/{t}"), ("DELETE", "/tenants/{t}")}
CASES = sorted(
    (
        (key, entry)
        for key, entry in ROUTES.items()
        if isinstance(entry, tuple) and key not in NOT_REQUESTED
    ),
    key=lambda case: case[0] in LAST,
)
ROLES = [Role.VIEWER, Role.OPERATOR, Role.TENANT_ADMIN, Role.SYSTEM_ADMIN]


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
async def test_status_codes_follow_the_matrix(api: Api, role: Role) -> None:
    headers = await api.token(role)
    failures: list[str] = []
    for (method, template), (verb, obj) in CASES:
        required = permissions.min_role(verb, obj)
        response = await asyncio.wait_for(
            api.client.request(
                method,
                V1 + fill(template) + query_for((method, template)),
                headers=headers | {"If-Match": '"1"'},
                json=body_for((method, template)),
            ),
            timeout=30,
        )
        denied = permissions.ROLE_RANK[role] < permissions.ROLE_RANK[required]
        where = f"{method} {template} as {role.value}"
        if (method, template) in TOKEN_MAKERS:
            # a token cannot create tokens, whatever its role
            if response.status_code != 403 or response.json().get("code") not in {
                "forbidden",
                "role_required",
            }:
                failures.append(f"{where}: expected 403 forbidden, got {response.status_code}")
        elif response.status_code == 503 and (method, template) in NOT_CONFIGURED:
            continue
        elif denied:
            if response.status_code != 403:
                failures.append(
                    f"{where}: expected 403, got {response.status_code} {response.text[:300]}"
                )
            else:
                problem = response.json()
                if (
                    problem.get("code") != "role_required"
                    or problem.get("required_role") != required.value
                ):
                    failures.append(f"{where}: 403 without the matrix role: {problem}")
        elif response.status_code == 403 and "reaching the new address" not in response.text:
            # confirming a controller change is refused on any host but the new one (ADR 0081)
            failures.append(f"{where}: allowed by the matrix but got 403 {response.text}")
        elif response.status_code >= 500:
            failures.append(f"{where}: {response.status_code}")
    assert not failures, "\n".join(failures)
