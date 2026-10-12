"""The Kubernetes planner: which Services and Gateways become which objects."""

from switchyard.connectors.kubernetes import ClusterSpec, object_name, plan, usable_nodes
from tests.k8s_support import endpoint_slice, gateway, http_route, node, service

NODES = [
    node("n1", "10.0.0.1"),
    node("n2", "10.0.0.2"),
    node("n3", "10.0.0.3", ready=False),
    node("n4", "10.0.0.4", labels={"node.kubernetes.io/exclude-from-external-load-balancers": ""}),
]
WEB = service(
    "shop",
    "web",
    [
        {"name": "http", "port": 80, "protocol": "TCP", "targetPort": 8080, "nodePort": 30080},
        {"name": "https", "port": 443, "protocol": "TCP", "targetPort": "https", "nodePort": 30443},
        {"name": "dns", "port": 53, "protocol": "UDP", "nodePort": 30053},
        {"name": "sctp", "port": 9, "protocol": "SCTP", "nodePort": 30009},
    ],
)
SLICES = [
    endpoint_slice(
        "shop",
        "web",
        [
            {"name": "http", "port": 8080},
            {"name": "https", "port": 8443},
            {"name": "dns", "port": 5353},
        ],
        [("10.1.0.5", "n1", True), ("10.1.0.6", "n2", True), ("10.1.0.7", "n2", False)],
    )
]


def notes(result: object) -> list[str]:
    return [f"{n.object}: {n.message}" for n in result.notes]  # type: ignore[attr-defined]


def test_only_services_of_our_class_and_type_are_planned() -> None:
    other = service("shop", "other", [{"port": 80, "nodePort": 31000}], klass="metallb")
    plain = service("shop", "plain", [{"port": 80}], kind="ClusterIP", klass=None)
    result = plan(ClusterSpec("prod"), NODES, [WEB, other, plain], SLICES)
    assert {s.source[2] for s in result.services.values()} == {"web"}


def test_nodeport_mode_pools_hold_the_ready_nodes_at_the_nodeport() -> None:
    result = plan(ClusterSpec("prod"), NODES, [WEB], SLICES)
    assert result.pools["k8s-prod-shop-web-80"].members == [
        ("10.0.0.1", 30080),
        ("10.0.0.2", 30080),
    ]
    svc = result.services["k8s-prod-shop-web-80"]
    assert (svc.protocol, svc.ports, svc.default_pool) == (
        "tcp",
        [{"port": 80}],
        "k8s-prod-shop-web-80",
    )
    assert result.services["k8s-prod-shop-web-53"].protocol == "udp"
    assert {s.vip_key for s in result.services.values()} == {"svc/shop/web"}, "one VIP per Service"
    assert result.status_targets == {"svc/shop/web": ("shop", "web")}
    assert any("SCTP is not supported" in n for n in notes(result))


def test_the_local_traffic_policy_keeps_only_nodes_that_host_endpoints() -> None:
    local = service("shop", "web", WEB["spec"]["ports"][:1], policy="Local")
    result = plan(ClusterSpec("prod"), NODES, [local], SLICES)
    assert result.pools["k8s-prod-shop-web-80"].members == [
        ("10.0.0.1", 30080),
        ("10.0.0.2", 30080),
    ]
    only_n2 = [
        endpoint_slice("shop", "web", [{"name": "http", "port": 8080}], [("10.1.0.6", "n2", True)])
    ]
    result = plan(ClusterSpec("prod"), NODES, [local], only_n2)
    assert result.pools["k8s-prod-shop-web-80"].members == [("10.0.0.2", 30080)]


def test_pods_mode_uses_ready_endpoints_and_resolves_named_target_ports() -> None:
    result = plan(ClusterSpec("prod", mode="pods"), NODES, [WEB], SLICES)
    assert result.pools["k8s-prod-shop-web-80"].members == [("10.1.0.5", 8080), ("10.1.0.6", 8080)]
    assert result.pools["k8s-prod-shop-web-443"].members == [("10.1.0.5", 8443), ("10.1.0.6", 8443)]
    assert result.pools["k8s-prod-shop-web-53"].members == [("10.1.0.5", 5353), ("10.1.0.6", 5353)]


def test_a_service_without_a_nodeport_is_explained_in_nodeport_mode() -> None:
    bare = service("shop", "bare", [{"port": 80, "protocol": "TCP"}])
    result = plan(ClusterSpec("prod"), NODES, [bare], [])
    assert not result.services
    assert any("no nodePort" in n for n in notes(result))


def test_an_empty_pool_is_planned_and_flagged() -> None:
    result = plan(ClusterSpec("prod", mode="pods"), NODES, [WEB], [])
    assert result.pools["k8s-prod-shop-web-80"].members == []
    assert any("no ready backend" in n for n in notes(result))


def test_usable_nodes_skip_unready_and_excluded() -> None:
    assert usable_nodes(NODES) == [("n1", "10.0.0.1"), ("n2", "10.0.0.2")]


GATEWAY = gateway("infra", "edge", [{"name": "http", "port": 80, "protocol": "HTTP"}])
BACKEND = service(
    "shop", "web", [{"name": "http", "port": 80, "nodePort": 30080}], kind="NodePort", klass=None
)


def test_gateway_and_routes_become_one_http_service_with_ordered_routes() -> None:
    routes = [
        http_route(
            "shop",
            "r1",
            "edge",
            [
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                    "backendRefs": [{"name": "web", "port": 80}],
                },
                {
                    "matches": [{"path": {"type": "Exact", "value": "/health"}}],
                    "backendRefs": [{"name": "web", "port": 80}],
                },
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": "/api"}}],
                    "backendRefs": [{"name": "web", "port": 80}],
                },
            ],
            hostnames=["shop.example.test"],
            parent_namespace="infra",
        )
    ]
    result = plan(ClusterSpec("prod"), NODES, [BACKEND], [], [GATEWAY], routes)
    svc = result.services["k8s-prod-gw-infra-edge"]
    assert svc.protocol == "http" and svc.ports == [{"port": 80}] and svc.vip_key == "gw/infra/edge"
    assert [r["match"] for r in svc.routes] == [
        {"path_exact": "/health", "host": "shop.example.test"},
        {"path_prefix": "/api", "host": "shop.example.test"},
        {"path_prefix": "/", "host": "shop.example.test"},
    ]
    assert svc.routes[0]["pool"] == "k8s-prod-gw-shop-web-80"
    assert result.pools["k8s-prod-gw-shop-web-80"].members == [
        ("10.0.0.1", 30080),
        ("10.0.0.2", 30080),
    ]


def test_gateway_without_routes_unattached_routes_and_foreign_classes() -> None:
    assert plan(ClusterSpec("prod"), NODES, [BACKEND], [], [GATEWAY], []).services == {}
    foreign = gateway("infra", "edge", [{"port": 80, "protocol": "HTTP"}], klass="istio")
    route = http_route(
        "shop",
        "r",
        "edge",
        [{"backendRefs": [{"name": "web", "port": 80}]}],
        parent_namespace="infra",
    )
    assert plan(ClusterSpec("prod"), NODES, [BACKEND], [], [foreign], [route]).services == {}
    elsewhere = http_route("shop", "r", "edge", [{"backendRefs": [{"name": "web", "port": 80}]}])
    assert plan(ClusterSpec("prod"), NODES, [BACKEND], [], [GATEWAY], [elsewhere]).services == {}, (
        "a route without a namespace in its parentRef points at its own namespace"
    )


def test_https_listeners_need_the_certificate_annotation_and_odd_matches_are_flagged() -> None:
    listeners = [
        {"port": 443, "protocol": "HTTPS"},
        {"port": 9000, "protocol": "TCP"},
        {"port": 80, "protocol": "HTTP"},
    ]
    no_cert = gateway("infra", "edge", listeners)
    with_cert = gateway("infra", "edge", listeners, certificate="shop-cert")
    route = http_route(
        "shop",
        "r",
        "edge",
        [
            {
                "matches": [{"headers": [{"name": "x"}]}],
                "backendRefs": [{"name": "web", "port": 80}],
            },
            {
                "matches": [{"path": {"type": "RegularExpression", "value": "/x"}}],
                "backendRefs": [{"name": "web", "port": 80}],
            },
            {
                "backendRefs": [
                    {"name": "web", "port": 80},
                    {"name": "web", "port": 80, "weight": 5},
                ]
            },
            {"backendRefs": [{"name": "ghost", "port": 80}]},
            {},
        ],
        parent_namespace="infra",
    )
    first = plan(ClusterSpec("prod"), NODES, [BACKEND], [], [no_cert], [route])
    assert first.services["k8s-prod-gw-infra-edge"].ports == [{"port": 80}]
    assert any("needs the annotation" in n for n in notes(first))
    second = plan(ClusterSpec("prod"), NODES, [BACKEND], [], [with_cert], [route])
    svc = second.services["k8s-prod-gw-infra-edge"]
    assert (
        svc.ports == [{"port": 443, "tls": True}, {"port": 80}] and svc.certificate == "shop-cert"
    )
    text = " | ".join(notes(second))
    for expected in (
        "header, query and method matches",
        "RegularExpression",
        "only the heaviest",
        "backend ghost not found",
        "without backends",
        "TCP listener 9000 is not supported",
    ):
        assert expected in text, expected


def test_names_stay_valid_and_unique_when_long() -> None:
    a = object_name("k8s-prod-", "n" * 50, "s" * 50, "80")
    b = object_name("k8s-prod-", "n" * 50, "s" * 50, "81")
    assert len(a) <= 63 and len(b) <= 63 and a != b
    assert object_name("k8s-p-", "Shop", "web.v2", "80") == "k8s-p-shop-web-v2-80"
