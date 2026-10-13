# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

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
        "deny_list_id": None,
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
        "renew_before_days": 30,
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
    "quotas": {"virtual_service": 50},
    "require_approval": True,
    "usage": {"virtual_service": 12, "pool": 20, "network": 2},
    "members": 7,
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
    "guest_kind": "qemu",
    "addresses": [{"network_id": NETWORK, "address": "192.0.2.31"}],
    "agent_enrolled": True,
    "agent_cert_serial": "3f2a9c",
    "host_online": True,
}
SYSTEM_SETTINGS = {"allow_lxc_guests": False, "allow_lxc_guests_forced": False}
SERVICE_ENGINES = [{"id": ENGINE, "name": "edge-1", "mgmt_ip": "192.0.2.21", "state": "enabled"}]
ENGINE_RENDERED = {
    "engine": "edge-1",
    "engine_id": ENGINE,
    "se_group_id": SE_GROUP,
    "fingerprint": "0" * 64,
    "files": [
        {
            "file": "haproxy/haproxy.cfg",
            "scope": "group",
            "language": "haproxy",
            "content": "global\n    maxconn 20000\n",
        },
        {
            "file": "frr/frr.conf",
            "scope": "engine",
            "language": "plaintext",
            "content": "router bgp 65000\n neighbor 192.0.2.1 password REDACTED\n",
        },
    ],
}
RUNNING_CONFIG = {
    "engine": "edge-1",
    "engine_id": ENGINE,
    "se_group_id": SE_GROUP,
    "reachable": True,
    "error": None,
    "haproxy": {
        "content": "global\n    maxconn 20000\n",
        "fingerprint": "0" * 64,
        "desired_fingerprint": "0" * 64,
        "fingerprint_matches": True,
        "matches_desired": True,
    },
    "agent": {
        "bundle_id": "b-1",
        "desired_bundle_id": "b-1",
        "files": [
            {
                "path": "frr/frr.conf",
                "sha256": "a" * 64,
                "desired_sha256": "a" * 64,
                "matches_desired": True,
            }
        ],
    },
    "fetched_at": WHEN,
}
PLACEMENT = {
    "available": True,
    "reason": None,
    "nodes": [
        {
            "name": "pve1",
            "online": True,
            "cpu": 0.12,
            "max_cpu": 32,
            "mem_used": 68719476736,
            "mem_total": 274877906944,
            "uptime_s": 1209600,
            "storage": [
                {
                    "name": "local-lvm",
                    "kind": "lvmthin",
                    "active": True,
                    "content": ["images", "rootdir"],
                    "total": 1099511627776,
                    "used": 219902325555,
                    "available": 879609302221,
                }
            ],
            "engines": [{"id": ENGINE, "name": "edge-1", "vmid": 9101, "state": "enabled"}],
        },
        {
            "name": "pve2",
            "online": False,
            "cpu": None,
            "max_cpu": None,
            "mem_used": None,
            "mem_total": None,
            "uptime_s": None,
            "storage": [],
            "engines": [],
        },
    ],
}
DISCOVERY = [
    {
        "targets": ["192.0.2.21:8405"],
        "labels": {
            "tenant": TENANT,
            "tenant_name": "acme",
            "se_group": "edge",
            "engine": "edge-1",
            "vs": ",shop,www,",
        },
    }
]
PRINCIPALS = [
    {"actor": "user:3f9a", "kind": "user", "display_name": "Alice Admin"},
    {"actor": "token:ci-bot", "kind": "token", "display_name": "ci-bot (API token)"},
]
VIRTUAL_SERVICE_STATUS = {
    "id": "00000000-0000-0000-0000-0000000000d1",
    "name": "dns",
    "protocol": "udp",
    "state": "serving",
    "health": "degraded",
    "members_up": 3,
    "members_total": 4,
    "members": [
        {
            "pool": "dns-pool",
            "pool_id": "00000000-0000-0000-0000-0000000000d2",
            "member_id": "00000000-0000-0000-0000-0000000000d3",
            "address": "10.20.0.53",
            "port": 53,
            "engine": "edge-1",
            "engine_id": ENGINE,
            "status": "DOWN",
            "check_status": "L4TOUT",
            "last_check": "Layer4 timeout",
            "last_change_s": 95,
            "last_checked_at": WHEN,
        }
    ],
}

SE_GROUP_HEALTH = {
    "se_group_id": SE_GROUP,
    "name": "edge",
    "score": 90,
    "reasons": ["1 of 2 engines not healthy"],
    "engines": [
        {"engine": "edge-1", "engine_id": ENGINE, "score": 100, "reasons": []},
        {"engine": "edge-2", "engine_id": ENGINE, "score": 80, "reasons": ["1 drifted file(s)"]},
    ],
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
        "waf_expected": False,
        "waf_running": False,
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
DEVICE_ENROLLMENT = {"token": "sydev_0123456789", "expires_at": "2026-10-11T09:30:00Z"}
DEVICE_ENROLLED = {
    "device_id": "0198b7c0-0000-7000-8000-000000000042",
    "tenant": "acme",
    "name": "laptop-ana",
    "policy": "managed-laptops",
    "cert_ttl_h": 12,
}
DEVICE_REPORT_OUT = {
    "compliant": True,
    "failures": [],
    "certificate": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n",
    "ca": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n",
    "not_after": "2026-10-10T21:30:00Z",
    "next_report_s": 14400,
}
DEVICE = {
    "id": "0198b7c0-0000-7000-8000-000000000042",
    "name": "laptop-ana",
    "owner": "ana@example.test",
    "policy_id": "0198b7c0-0000-7000-8000-000000000007",
    "status": "active",
    "compliant": True,
    "failures": [],
    "facts": {
        "os": "windows",
        "os_version": "10.0.22631",
        "disk_encrypted": True,
        "firewall_enabled": True,
        "days_since_update": 6,
    },
    "last_report_at": "2026-10-10T09:30:00Z",
    "cert_serial": "5f3a",
    "cert_not_after": "2026-10-10T21:30:00Z",
    "enrolled_at": "2026-10-01T08:00:00Z",
    "revoked_at": None,
}
DEVICES = {"items": [DEVICE]}
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
    "links": {"grafana": "https://grafana.example.test", "grafana_embedded": True},
    "features": {"lxc_guests": False},
}
PERMISSIONS = {
    "roles": ["viewer", "operator", "tenant-admin", "system-admin"],
    "permissions": [
        {
            "verb": "read",
            "object": "tenant_data",
            "min_role": "viewer",
            "description": "Read everything in the tenant.",
            "group": "Read",
        },
        {
            "verb": "apply",
            "object": "change_set",
            "min_role": "operator",
            "description": "Apply a change set when the tenant does not require approval.",
            "group": "Change sets",
        },
        {
            "verb": "manage",
            "object": "engine",
            "min_role": "tenant-admin",
            "description": "Register, enable, disable, retire or remove service engines.",
            "group": "Infrastructure",
        },
    ],
}
SDN_STATUS = [
    {
        "network_id": "0192f3a0-0000-7000-8000-000000000061",
        "network": "lb",
        "state": "running",
        "job_id": JOB,
        "started_at": "2026-10-11T08:30:00Z",
        "finished_at": None,
        "error": None,
    }
]
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
        "deny_list_id",
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
            "check_status": "L7STS",
            "last_check": "Layer7 wrong status",
            "last_change_s": 95,
            "last_checked_at": WHEN,
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

GRAFANA_LAUNCH = {
    "url": "/grafana/launch?ticket=sygt_Qm9vdHN0cmFwLXRpY2tldC1leGFtcGxlLW9ubHk",
    "expires_at": WHEN,
    "tenant_id": TENANT,
}

ALERT_TARGET = {
    "id": "0192f3a0-0000-7000-8000-0000000000a1",
    "name": "ops-chat",
    "kind": "chat",
    "config": {"url": "https://hooks.example.test/services/T000/B000/XXXX", "format": "slack"},
    "secret_ref": None,
    "enabled": True,
    "version": 1,
    "created_by": "user:authentik:alice",
    "created_at": WHEN,
}

ALERT_RULE = {
    "id": "0192f3a0-0000-7000-8000-0000000000a2",
    "name": "certs-expiring",
    "kind": "cert_expiry",
    "spec": {"days": [30, 14, 7]},
    "targets": ["ops-chat"],
    "enabled": True,
    "version": 1,
    "created_by": "user:authentik:alice",
    "created_at": WHEN,
}

ALERT_STATUS = {
    "firing": [
        {
            "rule": "certs-expiring",
            "key": "cert:shop-example",
            "state": "firing",
            "since": WHEN,
            "details": {"tier": 14, "not_after": WHEN},
        }
    ],
    "deliveries": [
        {
            "rule": "certs-expiring",
            "target": "ops-chat",
            "key": "cert:shop-example",
            "kind": "firing",
            "status": "sent",
            "error": None,
            "at": WHEN,
        }
    ],
}

ALERT_CATALOG = {
    "metrics": [
        {"name": "vs_5xx_ratio", "label": "Share of 5xx responses", "unit": "%", "scope": "vs"}
    ],
    "event_actions": ["change_set.failed", "drift.detected"],
    "chat_formats": ["slack", "teams", "mattermost"],
    "email_configured": False,
}

GSLB_ZONE = {
    "id": "0192f3a0-0000-7000-8000-0000000000b1",
    "name": "gslb.example.test",
    "nameservers": ["ns1.example.test", "ns2.example.test"],
    "dnssec": False,
    "version": 1,
    "created_by": "user:authentik:root",
    "created_at": WHEN,
}

GSLB_DS = {
    "zone": "gslb.example.test",
    "dnssec": True,
    "ds": [
        "gslb.example.test. IN DS 12345 13 2 "
        "ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789"
    ],
}

WIDE_IP = {
    "id": "0192f3a0-0000-7000-8000-0000000000b2",
    "name": "shop",
    "zone": "gslb.example.test",
    "fqdn": "shop.gslb.example.test",
    "algorithm": "failover",
    "ttl": 30,
    "health": {
        "kind": "tcp",
        "port": 443,
        "scheme": None,
        "path": None,
        "match": None,
        "host": None,
        "timeout_ms": 1000,
    },
    "members": [
        {
            "address": "192.0.2.10",
            "virtual_service": None,
            "site": "pve1",
            "weight": 1,
            "priority": 1,
            "subnets": [],
        },
        {
            "address": None,
            "virtual_service": "shop",
            "site": "pve2",
            "weight": 1,
            "priority": 2,
            "subnets": [],
        },
    ],
    "enabled": True,
    "version": 1,
    "applied_at": WHEN,
    "last_error": None,
    "created_by": "user:authentik:alice",
    "created_at": WHEN,
}

WIDE_IP_STATE = {
    "fqdn": "shop.gslb.example.test",
    "state": "in_sync",
    "last_error": None,
    "applied_at": WHEN,
}

CAPTURE = {
    "id": "0192f9a0-0000-7000-8000-000000000042",
    "engine": "edge-1",
    "requested_by": "user:alice",
    "spec": {
        "interface": "any",
        "protocol": "tcp",
        "host": "192.0.2.20",
        "port": 443,
        "duration_s": 10,
        "max_packets": 1000,
        "snaplen": 0,
    },
    "state": "done",
    "packets": 214,
    "size_bytes": 48210,
    "truncated": False,
    "error": None,
    "created_at": WHEN,
    "finished_at": WHEN,
    "expires_at": WHEN,
}

AUTOSCALE = {
    "se_group": "edge",
    "enabled": True,
    "engines": 3,
    "max_engines": 6,
    "scale_out_at": 70,
    "utilization": 41,
    "decision": "idle",
    "reason": None,
    "last_scaled_at": None,
}

DNS_RECORD = {
    "hostname": "app.example.test",
    "type": "A",
    "content": "192.0.2.20",
    "ttl": 300,
    "proxied": False,
    "state": "published",
    "virtual_service": "web",
    "last_error": None,
    "updated_at": WHEN,
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
ENGINE_METRICS = {
    "available": True,
    "reason": None,
    "metrics": {
        "window_s": 3600,
        "step_s": 30,
        "summary": {
            "cpu_percent": 32.0,
            "vcpus": 4,
            "memory_used_bytes": 2791728742,
            "memory_total_bytes": 8589934592,
            "haproxy_sessions": 1980,
            "traffic_in_bps": 147000000,
        },
        "cpu_percent": [{"t": WHEN, "v": 32.0}],
        "memory_used_bytes": [{"t": WHEN, "v": 2791728742}],
        "haproxy_sessions": [{"t": WHEN, "v": 1980}],
        "traffic_in_bps": [{"t": WHEN, "v": 147000000}],
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


DNS_PROVIDER_TYPES = [
    {
        "type": "cloudflare",
        "label": "Cloudflare",
        "supports_zone_discovery": True,
        "fields": [
            {
                "key": "api_token",
                "label": "API token",
                "kind": "text",
                "required": True,
                "secret": True,
                "help": "A token with Zone:Read and DNS:Edit on the zones you validate.",
                "options": [],
                "default": None,
            }
        ],
    }
]
DNS_PROVIDER = {
    "id": "0192f1a2-0000-7000-8000-00000000d501",
    "name": "cloudflare-main",
    "type": "cloudflare",
    "zones": ["example.test"],
    "config": {},
    "credentials_set": True,
    "enabled": True,
    "version": 1,
}
DNS_PROVIDER_TEST = {
    "ok": True,
    "detail": "every zone works",
    "zones": [
        {
            "zone": "example.test",
            "ok": True,
            "detail": "published and removed _switchyard-test.example.test",
        }
    ],
}
ISSUE_CHECK = {
    "ok": False,
    "problems": [{"path": "sans[1]", "message": "no DNS credential covers this name"}],
    "warnings": [],
}
RENEWAL = {
    "id": "0192f1a2-0000-7000-8000-00000000e501",
    "certificate": "web-example",
    "trigger": "auto",
    "started_at": "2026-10-12T02:00:00Z",
    "finished_at": "2026-10-12T02:01:10Z",
    "ok": True,
    "error": None,
    "change_set_id": "0192f1a2-0000-7000-8000-00000000c501",
    "not_after": "2027-01-10T02:01:00Z",
    "acme_provider": "letsencrypt",
    "challenge": "dns-01",
}
RENEWAL_STATUS = {
    "certificate_id": "0192f1a2-0000-7000-8000-00000000f501",
    "certificate": "web-example",
    "source": "acme",
    "issuer": "letsencrypt",
    "not_after": "2027-01-10T02:01:00Z",
    "renew_policy": "auto",
    "renew_before_days": 30,
    "renew_due_at": "2026-12-11T02:01:00Z",
    "last_attempt_at": "2026-10-12T02:00:00Z",
    "last_ok": True,
    "last_error": None,
    "last_success_at": "2026-10-12T02:01:10Z",
    "failures_in_a_row": 0,
}
DNS_MATCH = [
    {
        "name": "*.example.test",
        "candidates": [
            {
                "id": "0192f1a2-0000-7000-8000-00000000d501",
                "name": "cloudflare-main",
                "type": "cloudflare",
                "zone": "example.test",
            }
        ],
    }
]
ACME_PRESETS = [
    {
        "key": "letsencrypt",
        "label": "Let's Encrypt",
        "directory_url": "https://acme-v02.api.letsencrypt.org/directory",
        "auth_mode": "none",
        "eab_required": False,
        "note": "Free certificates, rate limited; use staging for tests.",
    }
]
ACME_PROVIDER = {
    "id": "0192f1a2-0000-7000-8000-00000000a501",
    "name": "letsencrypt",
    "preset": "letsencrypt",
    "directory_url": "https://acme-v02.api.letsencrypt.org/directory",
    "email": "pki@example.test",
    "auth_mode": "none",
    "eab_kid": None,
    "eab_configured": False,
    "oauth": None,
    "oauth_secret_set": False,
    "account_registered": True,
    "ca_bundle_set": False,
    "terms_agreed": True,
    "enabled": True,
    "default": True,
    "used_by": 3,
    "version": 1,
}
ACME_PROVIDER_TEST = {
    "ok": True,
    "detail": "account https://acme-v02.api.letsencrypt.org/acme/acct/1",
    "stages": [
        {"name": "directory", "ok": True, "detail": "reachable"},
        {
            "name": "account",
            "ok": True,
            "detail": "account https://acme-v02.api.letsencrypt.org/acme/acct/1",
        },
    ],
    "tos_url": "https://letsencrypt.org/documents/LE-SA-v1.5.pdf",
    "external_account_required": False,
}


CONTROLLER = {
    "managed": True,
    "management_tenant": True,
    "reached_at": "https://switchyard.example.test",
    "fqdn": "switchyard.example.test",
    "public_url": "https://switchyard.example.test",
    "listen": {
        "vip": "192.0.2.10",
        "ports": [
            {"port": 443, "tls": True, "https_redirect_port": None},
            {"port": 80, "tls": False, "https_redirect_port": 443},
        ],
    },
    "certificate": {
        "name": "controller",
        "sans": ["switchyard.example.test"],
        "not_after": "2027-01-10T02:01:00Z",
    },
    "pending": {
        "state": "waiting",
        "proposed_by": "user:root",
        "proposed_at": "2026-10-12T05:00:00Z",
        "window_s": 120,
        "expires_at": "2026-10-12T05:02:20Z",
        "seconds_left": 95,
        "before": {
            "fqdn": "switchyard.example.test",
            "public_url": "https://switchyard.example.test",
            "vip": "192.0.2.10",
            "certificate": "controller",
        },
        "after": {
            "fqdn": "sy.example.test",
            "public_url": "https://sy.example.test",
            "vip": "192.0.2.11",
            "certificate": "controller-new",
        },
        "change_set_id": "0192f1a2-0000-7000-8000-00000000c601",
        "revision_before": 7,
        "revision_after": 8,
    },
    "last_result": {
        "kind": "confirmed",
        "at": "2026-10-01T09:00:00Z",
        "by": "user:root",
        "fqdn": "switchyard.example.test",
    },
    "redirect_urls": [
        {
            "provider": "authentik",
            "protocol": "oidc",
            "redirect_url": "https://sy.example.test/api/v1/auth/callback/authentik",
        }
    ],
    "break_glass": (
        "On a controller node: `syctl controller status` and `syctl controller revert` need only "
        "database access; the break-glass login still works."
    ),
}


AVI_IMPORT = {
    "dry_run": True,
    "change_set_id": None,
    "counts": {"health_monitor": 1, "pool": 2, "virtual_service": 1, "certificate": 1},
    "objects": [
        {"kind": "pool", "avi_name": "Keycloak Pool", "name": "keycloak-pool"},
        {"kind": "virtual_service", "avi_name": "External-ParentSNI", "name": "external-parentsni"},
    ],
    "vips": [
        {
            "avi_vsvip": "vip-20",
            "address": "10.60.3.20",
            "new_address": "192.0.2.20",
            "services": ["External-ParentSNI"],
        }
    ],
    "findings": [
        {
            "level": "manual",
            "object": "virtual_service:External-ParentSNI",
            "message": "WAF policy detect-only is not converted (no WAF yet)",
        }
    ],
}


CONTROLLER_MEMBER = {
    "id": "0192f1a2-0000-7000-8000-00000000e701",
    "name": "ctl-esxi",
    "address": "10.0.0.13",
    "platform": "esxi",
    "host": "esxi-01",
    "external": True,
    "nofailover": True,
    "patroni_url": "http://10.0.0.13:8008",
    "created_by": "user:miguel@example.test",
    "created_at": "2026-10-12T09:00:00Z",
    "version": 1,
    "status": {
        "database_role": "replica",
        "database_state": "streaming",
        "lag_bytes": 0,
        "timeline": 4,
        "nofailover": True,
    },
    "in_cluster": True,
}


CONTROLLER_MEMBERS = {
    "members": [CONTROLLER_MEMBER],
    "cluster_reachable": True,
    "cluster_error": None,
    "leader": "ctl-1",
}


QUOTA_REQUEST = {
    "id": "0192f1a2-0000-7000-8000-00000000d701",
    "tenant_id": "0192f1a2-0000-7000-8000-000000000001",
    "tenant": "acme",
    "resource": "virtual_service",
    "current_limit": 20,
    "requested_limit": 50,
    "reason": "Two product teams move onto the platform in November.",
    "state": "pending",
    "requested_by": "user:miguel@example.test",
    "requested_at": "2026-10-12T09:00:00Z",
    "granted_limit": None,
    "decided_by": None,
    "decided_at": None,
    "decision_note": None,
}


KUBERNETES_CLUSTER = {
    "id": "0192f1a2-0000-7000-8000-00000000b701",
    "name": "prod",
    "api_url": "https://k8s.example.test:6443",
    "ca_set": True,
    "se_group": "edge",
    "vip_pool": "main",
    "mode": "nodeport",
    "class_name": "switchyard.io/lb",
    "gateway_class": "switchyard",
    "enabled": True,
    "waiting_for_change_set": None,
    "last_sync_at": "2026-10-12T05:00:00Z",
    "last_sync_ok": True,
    "last_sync_error": None,
    "last_sync": {
        "planned_services": 3,
        "planned_pools": 3,
        "created": 1,
        "updated": 0,
        "deleted": 0,
        "change_set_id": "0192f1a2-0000-7000-8000-00000000c701",
        "addresses_written": 2,
        "notes": [],
    },
    "version": 2,
}
KUBERNETES_TEST = {
    "ok": True,
    "detail": "reachable, with the access the connector needs",
    "version": "v1.31.2",
    "nodes": 3,
    "services": 42,
    "gateway_api": True,
    "permissions": {"list nodes": True, "list services": True, "patch services/status": True},
    "problems": [],
}


CRS_RULES = {
    "crs_version": "4.8.0",
    "complete": False,
    "groups": [
        {
            "prefix": 942,
            "name": "SQL injection",
            "description": "SQL injection into queries.",
            "rules": [
                {
                    "id": 942100,
                    "name": "SQL injection attack detected via libinjection",
                    "paranoia_level": 1,
                }
            ],
        }
    ],
}


UPGRADE_CHECK = {
    "controller_version": "0.1.0",
    "latest_version": "0.1.0",
    "schema_current": "0041",
    "schema_head": "0041",
    "schema_state": "current",
    "engines": [
        {
            "engine_id": "0190a5f2-0000-7000-8000-000000000001",
            "name": "se-1",
            "tenant_id": "0190a5f2-0000-7000-8000-000000000002",
            "agent_version": "0.0.9",
        }
    ],
    "findings": [
        {
            "code": "agent_behind",
            "severity": "warning",
            "subject": "se-1",
            "message": "Agent 0.0.9 is behind 0.1.0.",
            "action": "Upgrade the engine to the latest template (runbooks/rolling-upgrade.md).",
        }
    ],
}


BACKUP_TARGET = {
    "name": "nightly",
    "target": {
        "kind": "s3",
        "endpoint": "https://s3.example.test",
        "region": "us-east-1",
        "bucket": "sy-backups",
        "prefix": "controller",
        "access_key_ref": "secret/backup/access-key",
        "secret_key_ref": "secret/backup/secret-key",
    },
    "passphrase_ref": "secret/backup/passphrase",
    "keep": 14,
}

BACKUP_RUN = {
    "id": "0190a5f2-0000-7000-8000-000000000010",
    "target": "nightly",
    "archive": "switchyard-0.1.0-20261013T040000Z.syb",
    "status": "ok",
    "error": None,
    "size_bytes": 48211,
    "sha256": "9f2c0e5c1d6b8a7e3f4a5b6c7d8e9f00112233445566778899aabbccddeeff00",
    "controller_version": "0.1.0",
    "schema_revision": "0046",
    "tenants": 3,
    "reason": "pre-upgrade",
    "actor": "user:admin",
    "created_at": "2026-10-13T04:00:00+00:00",
}

BACKUP_VERIFY = {
    "controller_version": "0.1.0",
    "schema_revision": "0046",
    "created_at": "2026-10-13T04:00:00+00:00",
    "tenants": [
        {"name": "acme", "virtual_services": 12, "pools": 14, "se_groups": 2, "certificates": 5}
    ],
}

BACKUP_RESTORE = {**BACKUP_VERIFY, "restored": True}

RELEASE_FEED = {
    "url": "https://git.example.test/switchyard/-/releases/permalink/latest/downloads/releases.json",
    "channel": "stable",
    "enabled": True,
    "trusted_keys": {},
    "checked_at": "2026-10-13T04:00:00+00:00",
    "ok": True,
    "error": None,
    "latest_version": "0.2.0",
    "releases": [
        {
            "version": "0.2.0",
            "channel": "stable",
            "date": "2026-10-20",
            "notes": {"new": ["Upgrade checks"], "action_required": ["Back up first"]},
            "upgrade_from": ["0.1.0"],
        }
    ],
}

ROLL = {
    "id": "0190a5f2-0000-7000-8000-000000000020",
    "se_group_id": "0190a5f2-0000-7000-8000-000000000021",
    "status": "paused",
    "reason": "se-1: awaiting_enrollment took longer than 3600s",
    "template_vmid": 9770,
    "target_version": "0.2.0",
    "steps": [
        {"old": "se-1", "new": "se-1b", "phase": "awaiting_enrollment"},
        {"old": "se-2", "new": None, "phase": "pending"},
    ],
    "log": ["se-1: deploying replacement", "se-1b: deployed"],
    "created_by": "user:admin",
    "created_at": "2026-10-13T04:00:00+00:00",
    "updated_at": "2026-10-13T05:00:00+00:00",
    "version": 3,
}
