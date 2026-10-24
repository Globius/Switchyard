# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Hash-based balancing (ADR 0224): the key field, the algorithms and the protocol rules."""

import copy
from typing import Any

import pytest

from switchyard.domain.field_rules import protocol_rules
from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.models import Algorithm, Protocol
from switchyard.domain.snapshot import validate_snapshot
from switchyard.domain.validators import validate_hash_key
from tests.factories import BASE


def problems(data: dict[str, Any]) -> list[tuple[str, str]]:
    return [(p.path, p.code) for p in validate_snapshot(snapshot_from_mapping(data))]


def test_the_counting_algorithms_need_no_key() -> None:
    assert problems(copy.deepcopy(BASE)) == []


def test_uri_hashes_the_path_and_needs_no_key() -> None:
    data = copy.deepcopy(BASE)
    data["pools"][0]["algorithm"] = "uri"
    assert problems(data) == []


@pytest.mark.parametrize("algorithm", ["hdr", "url_param"])
def test_a_request_attribute_algorithm_needs_a_key(algorithm: str) -> None:
    data = copy.deepcopy(BASE)
    data["pools"][0]["algorithm"] = algorithm
    assert problems(data) == [("pools.web.hash_key", "hash_key_required")]


@pytest.mark.parametrize("algorithm", ["roundrobin", "leastconn", "source", "random", "uri"])
def test_a_key_on_an_algorithm_that_ignores_it_is_unexpected(algorithm: str) -> None:
    data = copy.deepcopy(BASE)
    data["pools"][0].update(algorithm=algorithm, hash_key="X-Cache-Key")
    assert problems(data) == [("pools.web.hash_key", "hash_key_unexpected")]


def test_a_key_that_bypasses_the_model_is_still_a_field_error() -> None:
    data = copy.deepcopy(BASE)
    data["pools"][0]["algorithm"] = "hdr"
    snapshot = snapshot_from_mapping(data)
    pool = snapshot.pools[0].model_construct(
        **{**dict(snapshot.pools[0]), "algorithm": Algorithm.HDR, "hash_key": "x y"}
    )
    found = [
        (p.path, p.code) for p in validate_snapshot(snapshot.model_copy(update={"pools": (pool,)}))
    ]
    assert found == [("pools.web.hash_key", "hash_key_invalid")]


def test_a_tcp_pool_cannot_hash_a_request_attribute() -> None:
    data = copy.deepcopy(BASE)
    data["pools"][0]["algorithm"] = "uri"
    data["virtual_services"] = [
        {
            "id": "vs-ldap",
            "tenant_id": "t-acme",
            "se_group_id": "seg-1",
            "name": "ldap",
            "protocol": "tcp",
            "vip": "192.0.2.30",
            "ports": [{"port": 636}],
            "default_pool_id": "pool-web",
        }
    ]
    assert problems(data) == [("pools.web.algorithm", "hash_key_needs_http")]


@pytest.mark.parametrize(
    ("algorithm", "key"),
    [("uri", None), ("hdr", "X-Cache-Key"), ("url_param", "X-Cache-Key")],
)
def test_a_udp_pool_rejects_the_hash_algorithms(algorithm: str, key: str | None) -> None:
    data = copy.deepcopy(BASE)
    data["pools"][0].update(algorithm=algorithm, hash_key=key)
    data["virtual_services"] = [
        {
            "id": "vs-dns",
            "tenant_id": "t-acme",
            "se_group_id": "seg-1",
            "name": "dns",
            "protocol": "udp",
            "vip": "192.0.2.31",
            "ports": [{"port": 53}],
            "default_pool_id": "pool-web",
        }
    ]
    found = problems(data)
    assert ("pools.web.algorithm", "udp_algorithm_unsupported") in found
    if key is not None:
        assert ("pools.web.hash_key", "udp_field_not_allowed") in found


def test_the_schema_publishes_the_udp_algorithm_list_and_forbids_the_key() -> None:
    rules = protocol_rules(Protocol.UDP)
    assert rules.algorithms == (Algorithm.ROUNDROBIN, Algorithm.LEASTCONN, Algorithm.SOURCE)
    assert "hash_key" in rules.pool_forbidden
    assert protocol_rules(Protocol.HTTP).algorithms == tuple(Algorithm)


@pytest.mark.parametrize("key", ["X-Cache-Key", "tenant_id", "session.id", "a"])
def test_valid_hash_keys(key: str) -> None:
    assert validate_hash_key(key) == key


@pytest.mark.parametrize("key", ["", "x y", "hdr(x)", "%[req.hdr(host)]", "a" * 65, "a,b"])
def test_invalid_hash_keys(key: str) -> None:
    with pytest.raises(ValueError, match="hash key must be"):
        validate_hash_key(key)
