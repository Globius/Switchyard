"""Example response bodies, one per route, published in the OpenAPI document.

Mock servers (MSW) are generated from these, so they are realistic and every one is validated
against its response model by ``tests/unit/test_api_schemas.py``.
"""

from typing import Any

TENANT = "0192f3a0-0000-7000-8000-000000000001"
NETWORK = "0192f3a0-0000-7000-8000-000000000010"
VIP_POOL = "0192f3a0-0000-7000-8000-000000000011"
SE_GROUP = "0192f3a0-0000-7000-8000-000000000012"
VIRTUAL_SERVICE = "0192f3a0-0000-7000-8000-000000000013"
POOL = "0192f3a0-0000-7000-8000-000000000014"
MONITOR = "0192f3a0-0000-7000-8000-000000000015"
CERTIFICATE = "0192f3a0-0000-7000-8000-000000000016"
POLICY_SET = "0192f3a0-0000-7000-8000-000000000017"
ROUTING_PROFILE = "0192f3a0-0000-7000-8000-000000000018"
MEMBER = "0192f3a0-0000-7000-8000-000000000019"
CHANGE_SET = "0192f3a0-0000-7000-8000-000000000020"
JOB = "0192f3a0-0000-7000-8000-000000000021"
ENGINE = "0192f3a0-0000-7000-8000-000000000022"
API_TOKEN_ID = "0192f3a0-0000-7000-8000-000000000023"  # noqa: S105
WHEN = "2026-10-10T08:30:00Z"

_DRAFT = {"draft_status": "unchanged", "changed_fields": []}
_OWNED = {"tenant_id": TENANT, "version": 3}

#: One item per object type, as returned by the ``GET`` routes.
OBJECTS: dict[str, dict[str, Any]] = {
    "networks": {
        "id": NETWORK,
        "name": "lb-dmz",
        "cidr": "192.0.2.0/24",
        "gateway": "192.0.2.1",
        "sdn_zone": None,
        "vnet": None,
        "vlan_or_vni": 120,
        "managed": False,
        **_OWNED,
        **_DRAFT,
    },
    "vip-pools": {
        "id": VIP_POOL,
        "name": "dmz-vips",
        "network_id": NETWORK,
        "ranges": [{"start": "192.0.2.100", "end": "192.0.2.200"}],
        **_OWNED,
        **_DRAFT,
    },
    "se-groups": {
        "id": SE_GROUP,
        "name": "edge",
        "ha_mode": "vrrp",
        "networks": [{"network_id": NETWORK, "role": "vip"}],
        "maxconn": 20000,
        "canary_seconds": 30,
        "routing_profile_id": ROUTING_PROFILE,
        **_OWNED,
        **_DRAFT,
    },
    "virtual-services": {
        "id": VIRTUAL_SERVICE,
        "name": "web",
        "se_group_id": SE_GROUP,
        "protocol": "http",
        "enabled": True,
        "vip": "192.0.2.100",
        "vip_pool_id": VIP_POOL,
        "ports": [{"port": 80, "tls": False, "proxy_protocol": False, "https_redirect_port": None}],
        "routes": [],
        "default_pool_id": POOL,
        "certificate_ids": [],
        "policy_set_ids": [],
        "allow_list_id": None,
        "enforce_allow_list": False,
        "restore_client_ip_header": None,
        **_OWNED,
        **_DRAFT,
    },
    "pools": {
        "id": POOL,
        "name": "web-pool",
        "algorithm": "roundrobin",
        "persistence": None,
        "health_monitor_id": MONITOR,
        "tls": False,
        "tls_verify": False,
        "members": [
            {
                "id": MEMBER,
                "address": "198.51.100.10",
                "port": 8080,
                "weight": 100,
                "admin_state": "enabled",
                "name": "web-1",
            }
        ],
        **_OWNED,
        **_DRAFT,
    },
    "health-monitors": {
        "id": MONITOR,
        "name": "http-ok",
        "type": "http",
        "port": None,
        "interval_s": 5,
        "timeout_s": 3,
        "rise": 2,
        "fall": 3,
        "http": {"method": "GET", "path": "/healthz", "host": None, "expect_status": [200]},
        **_OWNED,
        **_DRAFT,
    },
    "certificates": {
        "id": CERTIFICATE,
        "name": "web-example",
        "source": "acme",
        "sans": ["www.example.test"],
        "not_after": "2027-01-08T00:00:00Z",
        "secret_ref": "kv/tenants/acme/certs/web-example",
        "renew_policy": "auto",
        **_OWNED,
        **_DRAFT,
    },
    "policy-sets": {
        "id": POLICY_SET,
        "kind": "redirect",
        "name": "legacy-paths",
        "rules": [
            {
                "match": {"host": None, "path_prefix": "/old", "path_exact": None},
                "redirect": {"location": "/new", "code": 301},
            }
        ],
        **_OWNED,
        **_DRAFT,
    },
    "routing-profiles": {
        "id": ROUTING_PROFILE,
        "kind": "vrrp",
        "name": "edge-vrrp",
        "vrid": 51,
        "interface": "eth1",
        "advert_interval_s": 1,
        "base_priority": 150,
        "priority_step": 10,
        "static_routes": [],
        **_OWNED,
        **_DRAFT,
    },
}

#: Create bodies (what a client posts), per collection.
CREATE_BODIES: dict[str, dict[str, Any]] = {
    slug: {
        k: v
        for k, v in obj.items()
        if k not in {"id", "tenant_id", "version"} and k not in {"draft_status", "changed_fields"}
    }
    for slug, obj in OBJECTS.items()
}

ACCEPTED = {"change_set_id": CHANGE_SET, "object_id": POOL, "job_id": None, "op_index": 0}
JOB_ACCEPTED = {"job_id": JOB}

TENANT_READ = {
    "id": TENANT,
    "name": "acme",
    "version": 2,
    "quotas": {"virtual_services": 50},
    "require_approval": True,
}
OP = {
    "object_type": "pool",
    "object_id": POOL,
    "action": "update",
    "body": {"algorithm": "leastconn"},
    "expected_version": 3,
}
CHANGE_SET_READ = {
    "id": CHANGE_SET,
    "state": "draft",
    "author": "user:alice",
    "summary": "Switch web-pool to leastconn",
    "base_revision": 12,
    "approver": None,
    "reject_comment": None,
    "error": None,
    "created_at": WHEN,
    "ops": [OP],
}
CHANGE_SET_SUMMARY = {
    "id": CHANGE_SET,
    "state": "draft",
    "author": "user:alice",
    "summary": "Switch web-pool to leastconn",
    "base_revision": 12,
    "created_at": WHEN,
    "op_count": 1,
}
DIFF = {
    "objects": [
        {
            "type": "pool",
            "id": POOL,
            "name": "web-pool",
            "status": "modified",
            "changed_fields": ["algorithm"],
        }
    ],
    "problems": [],
    "warnings": [],
    "impact": {
        "services": [
            {"id": VIRTUAL_SERVICE, "name": "shop", "vip": "192.0.2.100", "se_group_id": SE_GROUP}
        ],
        "vips": ["192.0.2.100"],
        "se_groups": [{"id": SE_GROUP, "name": "edge"}],
        "hostnames": ["shop.example.test"],
    },
    "se_groups": [
        {
            "id": SE_GROUP,
            "name": "edge",
            "error": None,
            "files": [
                {
                    "path": "haproxy/haproxy.cfg",
                    "status": "modified",
                    "diff": "--- a/haproxy/haproxy.cfg\n+++ b/haproxy/haproxy.cfg\n"
                    "@@ -10 +10 @@\n-    balance roundrobin\n+    balance leastconn\n",
                    "before": "backend pool_web\n    balance roundrobin\n",
                    "after": "backend pool_web\n    balance leastconn\n",
                }
            ],
        }
    ],
}
RENDERED = {
    "items": [
        {
            "se_group_id": SE_GROUP,
            "file": "haproxy/haproxy.cfg",
            "language": "haproxy",
            "content": "backend pool_web\n    mode http\n    balance leastconn\n",
            "fingerprint": "0" * 64,
            "range": [1, 3],
        }
    ]
}
REVISION_SUMMARY = {
    "number": 12,
    "author": "user:alice",
    "summary": "Switch web-pool to leastconn",
    "message": "Ticket OPS-412: leastconn evens out the long uploads.",
    "git_sha": "3f9c2ab",
    "created_at": WHEN,
}
REVISION_READ = REVISION_SUMMARY | {
    "snapshot": {
        "pools": [
            {
                k: v
                for k, v in OBJECTS["pools"].items()
                if k not in {"draft_status", "changed_fields"}
            }
        ]
    }
}
ENGINE_READ = {
    "id": ENGINE,
    "name": "edge-1",
    "url": "https://198.51.100.21:5555",
    "state": "enabled",
    "se_group_id": SE_GROUP,
    "mgmt_ip": "192.0.2.21",
    "agent_url": "192.0.2.21:7443",
    "pve_node": "pve1",
    "vmid": 9101,
    "agent_enrolled": True,
    "agent_cert_serial": "3f2a9c",
}
VIRTUAL_SERVICE_STATUS = {
    "id": "00000000-0000-0000-0000-0000000000d1",
    "name": "dns",
    "protocol": "udp",
    "state": "serving",
    "health": "degraded",
    "members_up": 3,
    "members_total": 4,
}

ENGINE_STATUS = {
    "engine": "edge-1",
    "engine_id": ENGINE,
    "se_group_id": SE_GROUP,
    "reachable": True,
    "error": None,
    "haproxy_fingerprint": "9d2e7c41",
    "members": [{"backend": "be_web-pool", "server": "web-1", "status": "UP"}],
    "agent": {
        "version": "1.0.0",
        "bundle_id": "b-7f31",
        "haproxy_healthy": True,
        "routes_withdrawn": False,
        "withdrawn_prefixes": [],
        "bgp": [],
        "vrrp": [{"name": "VI_edge", "state": "MASTER"}],
        "ipvs": [],
        "file_hashes": {"haproxy/haproxy.cfg": "sha256:ab12"},
    },
    "drift": [{"kind": "haproxy_config", "expected": "9d2e7c41", "actual": "00000000"}],
    "observed_at": WHEN,
}
JOB_READ = {
    "id": JOB,
    "kind": "apply_change_set",
    "state": "succeeded",
    "attempts": 1,
    "error": None,
    "result": {"revision": 13},
}
JOB_FULL = {
    "id": JOB,
    "kind": "apply_change_set",
    "state": "succeeded",
    "tenant_id": TENANT,
    "change_set_id": CHANGE_SET,
    "created_at": WHEN,
    "started_at": WHEN,
    "finished_at": WHEN,
    "attempts": 1,
    "max_attempts": 3,
    "error": None,
    "retrying": False,
    "ok": False,
    "result": {
        "ok": False,
        "change_set_id": CHANGE_SET,
        "revision_number": None,
        "error": "SE group edge: canary failed: pool web went from up to down",
        "groups": [],
    },
    "stages": [
        {"name": n, "state": st, "started_at": WHEN, "finished_at": WHEN, "error": err}
        for n, st, err in [
            ("lock", "succeeded", None),
            ("render", "succeeded", None),
            ("validate", "succeeded", None),
            ("canary", "failed", "SE group edge: canary failed: pool web went from up to down"),
        ]
    ]
    + [
        {"name": n, "state": "skipped", "started_at": None, "finished_at": None, "error": None}
        for n in ("rollout", "commit")
    ],
}
JOB_LOG = {
    "job_id": JOB,
    "lines": [{"at": WHEN, "level": "info", "message": "attempt 1 claimed by worker-1"}],
}
AUDIT_ACTION = {
    "action": "change_set.applied",
    "category": "change_set",
    "description": "A change set finished applying and became a revision.",
}
AUDIT_EVENT = {
    "at": WHEN,
    "tenant_id": TENANT,
    "actor": "user:alice",
    "action": "change_set.apply",
    "object": f"change_set:{CHANGE_SET}",
    "diff": None,
    "request_id": "b7d4c2e0a1f34b6e",
}
TOKEN_READ = {
    "id": API_TOKEN_ID,
    "name": "ci",
    "role": "operator",
    "revoked": False,
    "expires_at": "2027-01-08T00:00:00Z",
}
TOKEN_CREATED = {"id": API_TOKEN_ID, "token": "sy_0123456789abcdef", "role": "operator"}
MEMBER_STATE = {"state": "drain", "applied": ["edge-1", "edge-2"], "failed": {}}
STATE_OUT = {"state": "discarded"}
ENGINE_STATE_OUT = {"state": "disabled"}
ENROLLMENT_TOKEN = {"token": "sy_enroll_0123456789", "expires_at": "2026-10-10T09:30:00Z"}
ENROLL_OUT = {
    "engine": "edge-1",
    "certificate": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n",
    "ca": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n",
    "bundle_public_key": "MCowBQYDK2VwAyEA...",
    "renewal_token": "sy_renew_0123456789",
    "not_after": "2026-11-10T00:00:00Z",
}
AUTH_PROVIDERS = {
    "providers": [
        {"id": "0192f3a0-0000-7000-8000-000000000030", "name": "Authentik", "protocol": "oidc"}
    ],
    "break_glass": False,
}
ME = {
    "id": "0192f3a0-0000-7000-8000-000000000031",
    "principal": "user:authentik:alice",
    "name": "Alice Example",
    "email": "alice@example.test",
    "is_system": False,
    "break_glass": False,
    "auth": "session",
    "csrf_token": "c3JmLXRva2Vu",
    "session": {
        "expires_at": "2026-10-10T16:00:00Z",
        "idle_timeout_seconds": 1800,
        "grants_computed_at": "2026-10-10T12:00:30Z",
    },
    "tenants": [
        {
            "id": TENANT,
            "name": "acme",
            "role": "tenant-admin",
            "via": [{"provider": "authentik", "group": "acme-admins"}],
        }
    ],
}
PERMISSIONS = {
    "roles": ["viewer", "operator", "tenant-admin", "system-admin"],
    "permissions": [
        {
            "verb": "read",
            "object": "tenant_data",
            "min_role": "viewer",
            "description": "Read everything in the tenant.",
        },
        {
            "verb": "apply",
            "object": "change_set",
            "min_role": "operator",
            "description": "Apply a change set when the tenant does not require approval.",
        },
        {
            "verb": "manage",
            "object": "engine",
            "min_role": "tenant-admin",
            "description": "Register, enable, disable, retire or remove service engines.",
        },
    ],
}
ROLE_MAPPING = {
    "id": "0192f3a0-0000-7000-8000-000000000041",
    "provider_id": "0192f3a0-0000-7000-8000-000000000042",
    "provider": "authentik",
    "group_name": "acme-ops",
    "label": "Acme operators",
    "tenant_id": TENANT,
    "tenant": "acme",
    "role": "operator",
    "version": 1,
    "created_by": "user:authentik:alice",
    "created_at": "2026-10-10T12:00:00Z",
}
USER_READ = {
    "id": "0192f3a0-0000-7000-8000-000000000051",
    "provider_id": "0192f3a0-0000-7000-8000-000000000042",
    "provider": "authentik",
    "username": "alice",
    "email": "alice@example.test",
    "groups": ["acme-ops"],
    "first_login_at": "2026-10-01T08:00:00Z",
    "last_login_at": "2026-10-10T11:58:00Z",
    "last_seen_at": "2026-10-10T12:00:00Z",
    "disabled_at": None,
    "grants": [
        {
            "tenant_id": TENANT,
            "tenant": "acme",
            "role": "operator",
            "via": [{"provider": "authentik", "group": "acme-ops"}],
        }
    ],
}
USER_DETAIL = USER_READ | {"subject": "u-1", "active_sessions": 1}
SESSIONS_REVOKED = {"revoked": 2}
HEALTH = {"status": "ok"}
PROTOCOL_RULES = {
    "protocol": "udp",
    "service_forbidden": [
        "certificate_ids",
        "policy_set_ids",
        "allow_list_id",
        "enforce_allow_list",
        "restore_client_ip_header",
        "routes",
    ],
    "port_forbidden": ["tls", "proxy_protocol", "http2", "https_redirect_port"],
    "pool_forbidden": ["tls", "tls_verify", "persistence"],
    "algorithms": ["roundrobin", "leastconn", "source"],
    "monitor_types": ["udp"],
    "member_port_required": True,
}

VIP_POOL_USAGE = {
    "pool_id": VIP_POOL,
    "total": 99,
    "used": 1,
    "free": 98,
    "allocations": [
        {
            "address": "192.0.2.100",
            "virtual_service_id": VIRTUAL_SERVICE,
            "virtual_service_name": "shop",
            "enabled": True,
            "draft_status": "unchanged",
        }
    ],
    "next_free": "192.0.2.101",
}
NEXT_FREE = {"address": "192.0.2.101"}

SEARCH = {
    "query": "shop",
    "hits": [
        {
            "type": "virtual_service",
            "id": VIRTUAL_SERVICE,
            "name": "shop",
            "matched": "name",
            "detail": "192.0.2.100",
        }
    ],
    "truncated": False,
}
SUMMARY = {
    "services": {"total": 4, "healthy": 2, "degraded": 1, "down": 0, "unknown": 0, "disabled": 1},
    "failing_members": [
        {
            "engine": "edge-1",
            "engine_id": ENGINE,
            "backend": "pool_web",
            "server": "web-2",
            "status": "DOWN",
        }
    ],
    "drifted_engines": [
        {
            "engine": "edge-2",
            "engine_id": ENGINE,
            "se_group_id": SE_GROUP,
            "kinds": ["haproxy_config"],
        }
    ],
    "unreachable_engines": [],
    "expiring_certificates": [
        {"id": CERTIFICATE, "name": "shop", "not_after": "2026-10-25T00:00:00Z", "days_left": 14}
    ],
    "jobs": {"queued": 0, "running": 1, "failed": 0},
    "open_drafts": 2,
    "pending_approval": 0,
    "latest_revision": {
        "number": 42,
        "author": "user:alice",
        "summary": "Add shop",
        "created_at": WHEN,
    },
}

METRICS = {
    "available": True,
    "reason": None,
    "metrics": {
        "window_s": 3600,
        "step_s": 30,
        "summary": {
            "rps": 120.5,
            "p95_ms": 18.0,
            "error_5xx_ratio": 0.002,
            "active_connections": 42,
        },
        "rps": [{"t": WHEN, "v": 120.5}],
        "p95_ms": [{"t": WHEN, "v": 18.0}],
        "error_5xx_ratio": [{"t": WHEN, "v": 0.002}],
        "active_connections": [{"t": WHEN, "v": 42}],
    },
}
METRICS_OVERVIEW = {
    "available": True,
    "reason": None,
    "window_s": 900,
    "items": [
        {
            "virtual_service_id": VIRTUAL_SERVICE,
            "name": "shop",
            "summary": {
                "rps": 120.5,
                "p95_ms": 18.0,
                "error_5xx_ratio": 0.002,
                "active_connections": 42,
            },
        }
    ],
}
ACCESS_LOGS = {
    "available": True,
    "reason": None,
    "entries": [
        {
            "at": WHEN,
            "client_ip": "198.51.100.7",
            "frontend": "vs_shop",
            "backend": "pool_web",
            "server": "web-1",
            "status": 200,
            "bytes": 512,
            "duration_ms": 13,
            "method": "GET",
            "path": "/cart",
            "raw": "198.51.100.7:51514 [10/Oct/2026:08:30:00.123] vs_shop pool_web/web-1 "
            '0/0/1/12/13 200 512 - - ---- 1/1/0/0/0 0/0 "GET /cart HTTP/1.1"',
        }
    ],
    "next_cursor": None,
}

MONITOR_TEST = {
    "ok": True,
    "latency_ms": 12,
    "detail": "HTTP 200",
    "engine": "edge-1",
    "target": "10.20.0.11:8080",
    "protocol": "http",
}
MONITOR_TEST_BODY = {"pool_id": POOL, "member_id": MEMBER}
IDP_OIDC = {
    "protocol": "oidc",
    "id": "0192f3a0-0000-7000-8000-000000000042",
    "name": "authentik",
    "enabled": True,
    "version": 1,
    "created_at": "2026-10-01T08:00:00Z",
    "warnings": [],
    "issuer_url": "https://sso.example.test/application/o/switchyard",
    "client_id": "switchyard",
    "client_secret_ref": "kv/idp/authentik",
    "client_secret_set": True,
    "scopes": "openid profile email",
    "username_claim": "preferred_username",
    "email_claim": "email",
    "groups_claim": "groups",
}
IDP_SAML = {
    "protocol": "saml",
    "id": "0192f3a0-0000-7000-8000-000000000043",
    "name": "corp",
    "enabled": True,
    "version": 1,
    "created_at": "2026-10-01T08:00:00Z",
    "warnings": [],
    "idp_metadata_url": "https://idp.example.test/metadata",
    "idp_entity_id": "https://idp.example.test/entity",
    "sso_url": "https://idp.example.test/sso",
    "signing_certificates": ["-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----"],
    "sp_entity_id": "https://switchyard.example.test/api/v1/auth/saml/0192f3a0-0000-7000-8000-000000000043",
    "name_id_format": None,
    "sign_requests": False,
    "sp_signing_key_ref": None,
    "sp_signing_certificate": None,
    "attribute_username": "username",
    "attribute_email": "email",
    "attribute_groups": "groups",
    "acs_url": "https://switchyard.example.test/api/v1/auth/callback/0192f3a0-0000-7000-8000-000000000043",
    "sp_metadata_url": "https://switchyard.example.test/api/v1/auth/saml/0192f3a0-0000-7000-8000-000000000043/metadata",
}
IDP_TEST_STARTED = {
    "test_id": "0192f3a0-0000-7000-8000-000000000044",
    "authorize_url": "https://sso.example.test/authorize?client_id=switchyard&state=...",
}
IDP_TEST_RESULT = {
    "status": "done",
    "subject": "u-1",
    "username": "alice",
    "email": "alice@example.test",
    "groups": ["acme-ops"],
    "claims": {"sub": "u-1", "preferred_username": "alice", "groups": ["acme-ops"]},
    "would_grant": [
        {
            "tenant": "acme",
            "role": "operator",
            "via": {"provider": "authentik", "group": "acme-ops"},
        }
    ],
    "warnings": [],
    "error": None,
}
BREAK_GLASS = {
    "forced_by_deployment": False,
    "enabled": True,
    "enabled_until": "2026-10-10T16:00:00Z",
    "enabled_by": "user:authentik:alice",
    "reason": "SSO outage, incident 4711",
    "admin": {
        "username": "breakglass",
        "totp_confirmed": True,
        "created_at": "2026-10-01T08:00:00Z",
        "last_login_at": None,
        "locked_until": None,
    },
}
BREAK_GLASS_ENROLLMENT = {
    "username": "breakglass",
    "password": "shown-once-aaaaaaaaaaaaaaaa",
    "totp_secret": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
    "otpauth_uri": "otpauth://totp/Switchyard:breakglass?secret=JBSWY3DPEHPK3PXP&issuer=Switchyard",
}
SYSTEM_TOKEN_READ = {
    "id": "0192f3a0-0000-7000-8000-000000000045",
    "name": "bootstrap",
    "role": "system-admin",
    "revoked": False,
    "expires_at": "2026-11-09T12:00:00Z",
    "created_by": "user:authentik:alice",
    "created_at": "2026-10-10T12:00:00Z",
    "last_used_at": None,
}
SYSTEM_TOKEN_CREATED = {
    "id": "0192f3a0-0000-7000-8000-000000000045",
    "token": "sy_shown-once",
    "role": "system-admin",
    "expires_at": "2026-11-09T12:00:00Z",
}
