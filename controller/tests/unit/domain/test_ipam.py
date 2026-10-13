# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""IPAM: deterministic, never hands out unusable or used addresses."""

import ipaddress
import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

from switchyard.domain.ipam import PoolExhaustedError, allocate, candidates
from switchyard.domain.models import IpRange, Network, VipPool

T = uuid.uuid4()


def make(cidr: str, gateway: str | None, *ranges: tuple[str, str]) -> tuple[VipPool, Network]:
    net = Network(id=uuid.uuid4(), tenant_id=T, name="n", cidr=cidr, gateway=gateway)
    pool = VipPool(
        id=uuid.uuid4(),
        tenant_id=T,
        name="p",
        network_id=net.id,
        ranges=tuple(IpRange(start=a, end=b) for a, b in ranges),
    )
    return pool, net


def test_lowest_free_address_skips_gateway_network_and_broadcast() -> None:
    pool, net = make("192.0.2.0/29", "192.0.2.1", ("192.0.2.0", "192.0.2.7"))
    assert [str(a) for a in candidates(pool, net)] == [f"192.0.2.{i}" for i in range(2, 7)]
    used = [ipaddress.ip_address("192.0.2.2"), ipaddress.ip_address("192.0.2.4")]
    assert str(allocate(pool, net, used)) == "192.0.2.3"


def test_overlapping_ranges_do_not_repeat_addresses() -> None:
    pool, net = make(
        "192.0.2.0/24", None, ("192.0.2.10", "192.0.2.12"), ("192.0.2.11", "192.0.2.13")
    )
    assert [str(a) for a in candidates(pool, net)] == [f"192.0.2.{i}" for i in range(10, 14)]


def test_addresses_outside_the_network_are_never_allocated() -> None:
    pool, net = make("192.0.2.0/29", None, ("192.0.2.5", "192.0.3.9"))
    assert [str(a) for a in candidates(pool, net)] == ["192.0.2.5", "192.0.2.6"]


def test_exhausted_pool_raises() -> None:
    pool, net = make("192.0.2.0/30", None, ("192.0.2.1", "192.0.2.2"))
    used = list(candidates(pool, net))
    with pytest.raises(PoolExhaustedError):
        allocate(pool, net, used)


def test_point_to_point_and_ipv6() -> None:
    pool, net = make("192.0.2.0/31", None, ("192.0.2.0", "192.0.2.1"))
    assert len(list(candidates(pool, net))) == 2
    pool6, net6 = make("2001:db8::/64", "2001:db8::1", ("2001:db8::1", "2001:db8::3"))
    assert str(allocate(pool6, net6, [])) == "2001:db8::2"


@given(st.sets(st.integers(2, 6)))
def test_allocation_is_unused_and_deterministic(taken: set[int]) -> None:
    pool, net = make("192.0.2.0/29", "192.0.2.1", ("192.0.2.0", "192.0.2.7"))
    used = [ipaddress.ip_address(f"192.0.2.{i}") for i in taken]
    if len(taken) == 5:
        with pytest.raises(PoolExhaustedError):
            allocate(pool, net, used)
        return
    got = allocate(pool, net, used)
    assert got not in used and got == allocate(pool, net, list(reversed(used)))
    assert all(a in used for a in candidates(pool, net) if int(a) < int(got))
