"""A synthetic Avi configuration export for the importer tests (no real data, RFC 5737 addresses)."""

from typing import Any

from tests.certs_support import make_pem

BASE = "https://avi.example.test/api"


def ref(kind: str, name: str) -> str:
    """An Avi reference in the API's ``url#name`` form."""
    return f"{BASE}/{kind}/{kind}-{abs(hash(name)) % 10**8:08x}#{name}"


def cert(name: str, sans: tuple[str, ...], *, days: int = 3650, key: bool = True) -> dict[str, Any]:
    """An SSLKeyAndCertificate with a real certificate."""
    pem = make_pem(sans, days=days, started_days_ago=400 if days < 0 else 1)
    cert_pem, _, key_pem = pem.partition("-----BEGIN PRIVATE KEY-----")
    item: dict[str, Any] = {
        "name": name,
        "type": "SSL_CERTIFICATE_TYPE_VIRTUALSERVICE",
        "certificate": {
            "certificate": cert_pem.strip() + "\n",
            "subject": {"common_name": sans[0]},
            "subject_alt_names": list(sans[1:]),
            "not_after": "2038-01-01 00:00:00" if days > 0 else "2020-01-01 00:00:00",
        },
    }
    if key:
        item["key"] = "-----BEGIN PRIVATE KEY-----" + key_pem
    return item


def export() -> dict[str, Any]:
    """The export: an SNI parent with two children, an L4 service, policies and leftovers."""
    return {
        "META": {"version": "22.1.2"},
        "Tenant": [{"name": "admin"}],
        "ApplicationProfile": [
            {
                "name": "System-Secure-HTTP",
                "type": "APPLICATION_PROFILE_TYPE_HTTP",
                "http_profile": {"http2_enabled": True},
            },
            {"name": "System-L4-Application", "type": "APPLICATION_PROFILE_TYPE_L4"},
        ],
        "NetworkProfile": [
            {"name": "System-TCP-Proxy", "profile": {"type": "PROTOCOL_TYPE_TCP_PROXY"}},
            {"name": "System-UDP-Fast-Path", "profile": {"type": "PROTOCOL_TYPE_UDP_FAST_PATH"}},
        ],
        "SSLProfile": [
            {
                "name": "System-Standard",
                "accepted_versions": [
                    {"type": "SSL_VERSION_TLS1_2"},
                    {"type": "SSL_VERSION_TLS1_3"},
                ],
            }
        ],
        "SSLKeyAndCertificate": [
            cert("wild-origin", ("globi.example", "*.globi.example")),
            cert("old-le", ("old.globi.example",), days=-10),
            cert("no-key", ("nokey.globi.example",), key=False),
        ],
        "HealthMonitor": [
            {
                "name": "System-TCP",
                "type": "HEALTH_MONITOR_TCP",
                "send_interval": 10,
                "receive_timeout": 4,
                "successful_checks": 2,
                "failed_checks": 2,
            },
            {
                "name": "api-http",
                "type": "HEALTH_MONITOR_HTTP",
                "send_interval": 5,
                "receive_timeout": 3,
                "http_monitor": {
                    "http_request": "GET /healthz HTTP/1.0",
                    "http_response_code": ["HTTP_2XX"],
                },
            },
            {"name": "ext", "type": "HEALTH_MONITOR_EXTERNAL"},
        ],
        "ApplicationPersistenceProfile": [
            {
                "name": "ip-5",
                "persistence_type": "PERSISTENCE_TYPE_CLIENT_IP_ADDRESS",
                "ip_persistence_profile": {"ip_persistent_timeout": 10},
            },
            {"name": "cookie", "persistence_type": "PERSISTENCE_TYPE_HTTP_COOKIE"},
        ],
        "Pool": [
            {
                "name": "Keycloak Pool",
                "lb_algorithm": "LB_ALGORITHM_LEAST_CONNECTIONS",
                "default_server_port": 8080,
                "servers": [{"ip": {"addr": "10.60.1.5", "type": "V4"}}],
            },
            {
                "name": "billing",
                "lb_algorithm": "LB_ALGORITHM_ROUND_ROBIN",
                "servers": [
                    {"ip": {"addr": "10.60.1.2"}, "port": 443, "ratio": 1},
                    {"ip": {"addr": "10.60.1.3"}, "port": 443, "ratio": 3, "enabled": False},
                    {"hostname": "named.example.test", "port": 443},
                ],
                "health_monitor_refs": [ref("healthmonitor", "api-http")],
                "application_persistence_profile_ref": ref("applicationpersistenceprofile", "ip-5"),
                "ssl_profile_ref": ref("sslprofile", "System-Standard"),
            },
            {
                "name": "rke",
                "lb_algorithm": "LB_ALGORITHM_FASTEST_RESPONSE",
                "servers": [
                    {"ip": {"addr": "10.60.6.27"}, "port": 6443},
                    {"ip": {"addr": "10.60.6.28"}, "port": 6443},
                ],
                "health_monitor_refs": [ref("healthmonitor", "System-TCP")],
                "application_persistence_profile_ref": ref(
                    "applicationpersistenceprofile", "cookie"
                ),
            },
            {
                "name": "dns",
                "lb_algorithm": "LB_ALGORITHM_ROUND_ROBIN",
                "servers": [{"ip": {"addr": "10.60.0.5"}, "port": 51820}],
            },
            {"name": "leftover", "servers": [{"ip": {"addr": "10.1.1.1"}, "port": 80}]},
        ],
        "PoolGroup": [
            {
                "name": "billing-group",
                "members": [
                    {"pool_ref": ref("pool", "billing"), "ratio": 1},
                    {"pool_ref": ref("pool", "rke"), "ratio": 1},
                ],
            }
        ],
        "HTTPPolicySet": [
            {
                "name": "nextcloud-dav",
                "http_request_policy": {
                    "rules": [
                        {
                            "index": 1,
                            "name": "carddav",
                            "match": {
                                "path": {
                                    "match_criteria": "BEGINS_WITH",
                                    "match_str": ["/.well-known/carddav"],
                                }
                            },
                            "redirect_action": {
                                "protocol": "HTTPS",
                                "path": {
                                    "type": "URI_PARAM_TYPE_TOKENIZED",
                                    "tokens": [
                                        {
                                            "type": "URI_TOKEN_TYPE_STRING",
                                            "str_value": "/remote.php/dav",
                                        }
                                    ],
                                },
                                "status_code": "HTTP_REDIRECT_STATUS_CODE_301",
                            },
                        },
                        {
                            "index": 2,
                            "name": "to-api",
                            "match": {"path": {"match_criteria": "EQUALS", "match_str": ["/api"]}},
                            "switching_action": {
                                "action": "HTTP_SWITCHING_SELECT_POOL",
                                "pool_ref": ref("pool", "billing"),
                            },
                        },
                        {
                            "index": 3,
                            "name": "rewrite",
                            "match": {
                                "path": {"match_criteria": "BEGINS_WITH", "match_str": ["/x"]}
                            },
                            "rewrite_url_action": {"path": {}},
                        },
                        {
                            "index": 4,
                            "name": "https-only",
                            "match": {},
                            "redirect_action": {
                                "protocol": "HTTPS",
                                "status_code": "HTTP_REDIRECT_STATUS_CODE_302",
                            },
                        },
                    ]
                },
            }
        ],
        "VsVip": [
            {"name": "vip-20", "vip": [{"ip_address": {"addr": "10.60.3.20", "type": "V4"}}]},
            {"name": "vip-27", "vip": [{"ip_address": {"addr": "10.60.3.27"}}]},
            {"name": "vip-22", "vip": [{"ip_address": {"addr": "10.60.3.22"}}]},
            {"name": "vip-99", "vip": [{"ip_address": {"addr": "10.60.3.99"}}]},
        ],
        "VirtualService": [
            {
                "name": "External-ParentSNI",
                "type": "VS_TYPE_VH_PARENT",
                "vsvip_ref": ref("vsvip", "vip-20"),
                "application_profile_ref": ref("applicationprofile", "System-Secure-HTTP"),
                "network_profile_ref": ref("networkprofile", "System-TCP-Proxy"),
                "ssl_profile_ref": ref("sslprofile", "System-Standard"),
                "ssl_key_and_certificate_refs": [ref("sslkeyandcertificate", "wild-origin")],
                "services": [{"port": 443, "enable_ssl": True}, {"port": 80}],
                "waf_policy_ref": ref("wafpolicy", "detect-only"),
                "http_policies": [
                    {"index": 11, "http_policy_set_ref": ref("httppolicyset", "nextcloud-dav")}
                ],
                "enabled": True,
            },
            {
                "name": "Keycloak SSO",
                "type": "VS_TYPE_VH_CHILD",
                "vh_parent_vs_ref": ref("virtualservice", "External-ParentSNI"),
                "vh_domain_name": ["sso.globi.example", "sso-admin.globi.example"],
                "pool_ref": ref("pool", "Keycloak Pool"),
            },
            {
                "name": "Billing",
                "type": "VS_TYPE_VH_CHILD",
                "vh_parent_vs_ref": ref("virtualservice", "External-ParentSNI"),
                "vh_domain_name": ["billing.globi.example"],
                "pool_group_ref": ref("poolgroup", "billing-group"),
                "ssl_key_and_certificate_refs": [
                    ref("sslkeyandcertificate", "no-key"),
                    ref("sslkeyandcertificate", "old-le"),
                ],
            },
            {
                "name": "RKE API",
                "type": "VS_TYPE_NORMAL",
                "vsvip_ref": ref("vsvip", "vip-27"),
                "application_profile_ref": ref("applicationprofile", "System-L4-Application"),
                "network_profile_ref": ref("networkprofile", "System-TCP-Proxy"),
                "pool_ref": ref("pool", "rke"),
                "services": [{"port": 6443}, {"port": 9345, "port_range_end": 9346}],
                "vs_datascripts": [{"vs_datascript_set_ref": ref("vsdatascriptset", "legacy")}],
            },
            {
                "name": "WireGuard",
                "type": "VS_TYPE_NORMAL",
                "vsvip_ref": ref("vsvip", "vip-22"),
                "application_profile_ref": ref("applicationprofile", "System-L4-Application"),
                "network_profile_ref": ref("networkprofile", "System-UDP-Fast-Path"),
                "pool_ref": ref("pool", "dns"),
                "services": [{"port": 51820}],
            },
            {
                "name": "Unused EVH",
                "type": "VS_TYPE_VH_ENHANCED",
                "vsvip_ref": ref("vsvip", "vip-99"),
                "services": [{"port": 80}],
            },
        ],
    }
