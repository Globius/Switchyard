# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit and property tests for the domain validators."""

import ipaddress

import pytest
from hypothesis import given
from hypothesis import strategies as st

from switchyard.domain import validators as v

LABEL = st.from_regex(r"[a-z0-9]([a-z0-9-]{0,10}[a-z0-9])?", fullmatch=True)
HOSTNAME = st.lists(LABEL, min_size=1, max_size=4).map(".".join).filter(lambda h: not _is_ip(h))


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


class TestHostname:
    @given(HOSTNAME)
    def test_valid_hostnames_round_trip(self, host: str) -> None:
        assert v.validate_hostname(host) == host
        assert v.validate_hostname(host.upper()) == host

    @given(HOSTNAME)
    def test_validation_is_idempotent(self, host: str) -> None:
        once = v.validate_hostname(host)
        assert v.validate_hostname(once) == once

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "a..b",
            ".a",
            "a.",
            "-a.example.test",
            "a-.example.test",
            "a_b.example.test",
            "a b.example.test",
            "a.example.test\nfoo",
            "a.example.test;rm",
            "*.example.test",
            "192.0.2.1",
            "::1",
            "é.example.test",
            "a" * 64 + ".test",
            ".".join(["a" * 60] * 5),
        ],
    )
    def test_rejects(self, bad: str) -> None:
        with pytest.raises(ValueError):
            v.validate_hostname(bad)

    @given(st.text(max_size=40))
    def test_never_accepts_config_metacharacters(self, text: str) -> None:
        try:
            result = v.validate_hostname(text)
        except ValueError:
            return
        assert all(c.isalnum() or c in "-." for c in result)
        assert result.isascii()


class TestHostPattern:
    @given(HOSTNAME)
    def test_wildcard_accepted(self, host: str) -> None:
        assert v.validate_host_pattern("*." + host) == "*." + host

    @pytest.mark.parametrize("bad", ["*", "*.", "a.*.test", "**.test", "*a.test", "*.*.test"])
    def test_rejects(self, bad: str) -> None:
        with pytest.raises(ValueError):
            v.validate_host_pattern(bad)


class TestCertificateCoverage:
    @given(HOSTNAME, LABEL)
    def test_wildcard_covers_exactly_one_label(self, domain: str, label: str) -> None:
        san = "*." + domain
        assert v.host_covered_by_san(f"{label}.{domain}", san)
        assert not v.host_covered_by_san(domain, san)
        assert not v.host_covered_by_san(f"x.{label}.{domain}", san)

    @given(HOSTNAME)
    def test_exact_san_covers_itself_only(self, host: str) -> None:
        assert v.host_covered_by_san(host, host)
        assert not v.host_covered_by_san("x." + host, host)

    @given(HOSTNAME)
    def test_wildcard_host_needs_identical_wildcard_san(self, host: str) -> None:
        assert v.host_covered_by_san("*." + host, "*." + host)
        assert not v.host_covered_by_san("*." + host, host)
        assert not v.host_covered_by_san("*.x." + host, "*." + host)

    def test_case_insensitive(self) -> None:
        assert v.host_covered_by_san("A.Example.Test", "*.EXAMPLE.test")

    def test_suffix_trick_is_not_covered(self) -> None:
        # "evilexample.test" ends with "example.test" but not with ".example.test".
        assert not v.host_covered_by_san("evilexample.test", "*.example.test")

    def test_uncovered_hosts_sorted_and_unique(self) -> None:
        out = v.uncovered_hosts(
            ["z.other.test", "a.other.test", "z.other.test"], ["*.example.test"]
        )
        assert out == ["a.other.test", "z.other.test"]


class TestPort:
    @given(st.integers(min_value=1, max_value=65535))
    def test_valid(self, port: int) -> None:
        assert v.validate_port(port) == port

    @given(st.one_of(st.integers(max_value=0), st.integers(min_value=65536)))
    def test_out_of_range(self, port: int) -> None:
        with pytest.raises(ValueError):
            v.validate_port(port)

    def test_bool_rejected(self) -> None:
        with pytest.raises(ValueError):
            v.validate_port(True)


class TestPathAndLocation:
    @given(st.from_regex(r"/[A-Za-z0-9._~/%:@+,;=-]{0,40}", fullmatch=True))
    def test_valid_paths(self, path: str) -> None:
        assert v.validate_path(path) == path

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "a",
            "/a b",
            "/a\n",
            '/a"',
            "/a'",
            "/a#b",
            "/a\\b",
            "/a$b",
            "/a{b}",
            "/a(b)",
            "/a[b]",
            "/a?b",
        ],
    )
    def test_rejected_paths(self, bad: str) -> None:
        with pytest.raises(ValueError):
            v.validate_path(bad)

    @pytest.mark.parametrize(
        "ok", ["/x", "/remote.php/dav/", "https://files.example.test/dav?x=1&y=2"]
    )
    def test_valid_locations(self, ok: str) -> None:
        assert v.validate_location(ok) == ok

    @pytest.mark.parametrize(
        "bad",
        [
            "//evil.test/x",
            "javascript:alert(1)",
            "ftp://x.test/",
            "https://a b.test/",
            "https://x.test:99999/",
            "/x y",
            "/x#frag",
            'https://x.test/"',
            "https://x.test/\n",
        ],
    )
    def test_rejected_locations(self, bad: str) -> None:
        with pytest.raises(ValueError):
            v.validate_location(bad)

    def test_location_host_lowercased(self) -> None:
        assert v.validate_location("https://A.Example.Test/p") == "https://a.example.test/p"


class TestMisc:
    @pytest.mark.parametrize("ok", ["a", "web-prod", "a_b-c", "x" * 63])
    def test_names(self, ok: str) -> None:
        assert v.validate_name(ok) == ok

    @pytest.mark.parametrize("bad", ["", "-a", "a-", "A", "a b", "a/b", "x" * 64, "a\nb"])
    def test_bad_names(self, bad: str) -> None:
        with pytest.raises(ValueError):
            v.validate_name(bad)

    @pytest.mark.parametrize("ok", ["X-Real-IP", "CF-Connecting-IP", "a"])
    def test_headers(self, ok: str) -> None:
        assert v.validate_header_name(ok) == ok

    @pytest.mark.parametrize("bad", ["", "X Real", "X:Real", "-x", "x-", "X\nY", "a" * 65])
    def test_bad_headers(self, bad: str) -> None:
        with pytest.raises(ValueError):
            v.validate_header_name(bad)

    @pytest.mark.parametrize("bad", ["../x", "a//b", "A/b", "", "/abs", "a b"])
    def test_bad_secret_refs(self, bad: str) -> None:
        with pytest.raises(ValueError):
            v.validate_secret_ref(bad)


class TestCheckRegex:
    @pytest.mark.parametrize(
        "ok",
        [
            "^2[0-9][0-9]$",
            "^v[0-9]+[.][0-9]+[.][0-9]+$",
            '"version":"v[0-9]+"',
            "^ok$",
            "x" * v.CHECK_REGEX_MAX,
        ],
    )
    def test_accepts_anchored_and_json_patterns(self, ok: str) -> None:
        assert v.validate_check_regex(ok) == ok

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "x" * (v.CHECK_REGEX_MAX + 1),
            "(unclosed",
            "[z-a]",
            "*leading",
            "%[req.hdr(x)]",
            "%{+Q}o",
            "it's",
            "a\nb",
            "a\tb",
        ],
    )
    def test_rejects(self, bad: str) -> None:
        with pytest.raises(ValueError):
            v.validate_check_regex(bad)

    def test_rejection_reason_is_specific(self) -> None:
        with pytest.raises(ValueError, match="log-format variable"):
            v.validate_check_regex("%[req.hdr(x)]")
        with pytest.raises(ValueError, match="does not compile"):
            v.validate_check_regex("(unclosed")
        with pytest.raises(ValueError, match="1-256 characters"):
            v.validate_check_regex("x" * (v.CHECK_REGEX_MAX + 1))


class TestVipInNetwork:
    NET = ipaddress.ip_network("192.0.2.0/24")

    @given(st.integers(min_value=1, max_value=254))
    def test_hosts_inside_are_valid(self, last: int) -> None:
        v.validate_vip_in_network(ipaddress.ip_address(f"192.0.2.{last}"), self.NET)

    @given(st.integers(min_value=0, max_value=2**32 - 1).filter(lambda n: n >> 8 != 0xC00002))
    def test_addresses_outside_are_rejected(self, n: int) -> None:
        with pytest.raises(ValueError, match="outside"):
            v.validate_vip_in_network(ipaddress.IPv4Address(n), self.NET)

    def test_network_and_broadcast_rejected(self) -> None:
        for bad in ("192.0.2.0", "192.0.2.255"):
            with pytest.raises(ValueError):
                v.validate_vip_in_network(ipaddress.ip_address(bad), self.NET)

    def test_point_to_point_prefixes_allow_both_addresses(self) -> None:
        net = ipaddress.ip_network("192.0.2.0/31")
        v.validate_vip_in_network(ipaddress.ip_address("192.0.2.0"), net)
        v.validate_vip_in_network(ipaddress.ip_address("192.0.2.1"), net)

    def test_family_mismatch(self) -> None:
        with pytest.raises(ValueError, match="IPv6"):
            v.validate_vip_in_network(ipaddress.ip_address("2001:db8::1"), self.NET)

    def test_ipv6_network_address_rejected_but_last_allowed(self) -> None:
        net = ipaddress.ip_network("2001:db8::/64")
        with pytest.raises(ValueError):
            v.validate_vip_in_network(ipaddress.ip_address("2001:db8::"), net)
        v.validate_vip_in_network(ipaddress.ip_address("2001:db8::ffff:ffff:ffff:ffff"), net)


class TestPortCollisions:
    @given(
        st.lists(
            st.tuples(
                st.sampled_from(["a", "b", "c"]),
                st.integers(min_value=0, max_value=3).map(
                    lambda i: ipaddress.ip_address(f"192.0.2.{i + 1}")
                ),
                st.integers(min_value=1, max_value=4),
            ),
            unique=True,
            max_size=12,
        )
    )
    def test_collisions_are_exactly_duplicate_bindings(
        self, items: list[tuple[str, ipaddress.IPv4Address, int]]
    ) -> None:
        listeners = [v.Listener(o, ip, p) for o, ip, p in items]
        found = v.find_port_collisions(listeners)
        keys = [(ip, p) for _, ip, p in items]
        expected = len(keys) - len(set(keys))
        assert len(found) == expected
        for first, second in found:
            assert (first.vip, first.port) == (second.vip, second.port)

    def test_tcp_and_udp_on_same_port_do_not_collide(self) -> None:
        ip = ipaddress.ip_address("192.0.2.1")
        assert not v.find_port_collisions(
            [v.Listener("a", ip, 53, "tcp"), v.Listener("b", ip, 53, "udp")]
        )

    def test_order_independent(self) -> None:
        ip = ipaddress.ip_address("192.0.2.1")
        a, b = v.Listener("a", ip, 443), v.Listener("b", ip, 443)
        assert v.find_port_collisions([a, b]) == v.find_port_collisions([b, a])
