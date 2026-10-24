# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The rendered ``log-format``, the engine's Vector regex and the controller's parser cannot drift.

The test renders the HAProxy access-log format from :data:`switchyard.render.haproxy.LOG_FORMAT`
with one sample value per tag (failing on a tag it does not know, so a format change has to be
acknowledged here), then parses the line with the exact regex the shipped
``se-image/files/vector.yaml`` uses — read from that file, not copied — and with
:func:`switchyard.adapters.telemetry.parse_httplog`. One format, one set of field names, three
readers.
"""

import re
from pathlib import Path

import pytest

from switchyard.adapters.telemetry import parse_httplog
from switchyard.render.haproxy import LOG_FORMAT
from tests.golden_support import expected_files

ROOT = Path(__file__).parents[4]  # repository root, next to controller/ and se-image/
VECTOR = ROOT / "se-image" / "files" / "vector.yaml"
CASE = "35-access-log-format"

#: What HAProxy writes for each tag of the format on one TLS request to ``vs_shop``.
SAMPLES = {
    "%ci": "192.0.2.7",
    "%cp": "51514",
    "%tr": "10/Oct/2026:08:30:00.123",
    "%f": "vs_shop",
    "%b": "pool_shop",
    "%s": "10-20-3-11-8080",
    "%TR": "0",
    "%Tw": "0",
    "%Tc": "1",
    "%Tr": "12",
    "%Ta": "13",
    "%ST": "200",
    "%B": "512",
    "%ID": "6f5b0f7e-6e0a-4a4f-9d2b-2b6d2f9a1c33",
    "%{+Q}[ssl_fc_sni]": '"shop.example.test"',
    "%{+Q}[var(txn.host)]": '"shop.example.test"',
    "%[bc_dst]": "10.20.3.11",
    "%[bc_dst_port]": "8080",
    "%rc": "1",
    "%tsc": "----",
    "%ac": "2",
    "%fc": "2",
    "%bc": "1",
    "%sc": "1",
    "%sq": "0",
    "%bq": "0",
    "%hr": "",
    "%hs": "",
    "%{+Q}r": '"GET /cart?x=1 HTTP/1.1"',
}

TAG = re.compile(r"%\{[^}]*\}\[[^\]]*\]|%\{[^}]*\}[A-Za-z]|%\[[^\]]*\]|%[A-Za-z]+")


def vector_pattern() -> re.Pattern[str]:
    """The regex of the engine's Vector ``parse`` transform, read from the shipped config."""
    for line in VECTOR.read_text("utf-8").splitlines():
        if "parse_regex(msg, r'" in line:
            return re.compile(line.split("parse_regex(msg, r'", 1)[1].rsplit("'", 1)[0])
    raise AssertionError("se-image/files/vector.yaml has no parse_regex in its parse transform")


def rendered_line() -> str:
    """One log line as the rendered format writes it (an unknown tag fails the test)."""

    def sample(match: re.Match[str]) -> str:
        tag = match.group(0)
        if tag not in SAMPLES:
            raise AssertionError(f"log-format tag {tag} has no sample value in this test")
        return SAMPLES[tag]

    return TAG.sub(sample, LOG_FORMAT)


def test_the_format_is_the_one_the_golden_case_renders() -> None:
    cfg = expected_files(CASE)["haproxy/haproxy.cfg"]
    assert f'    log-format "{LOG_FORMAT}"' in cfg
    # HAProxy generates the request id itself; the controller is not in the path.
    assert "    unique-id-format %[uuid()]" in cfg
    assert "option httplog" in cfg


def test_the_formats_fields_are_the_ones_the_parsers_expect() -> None:
    line = rendered_line()
    # the whole line is one record: a newline in a value would split it
    assert "\n" not in line
    parsed = vector_pattern().search(line)
    assert parsed is not None
    assert parsed.groupdict() == {
        "client": "192.0.2.7",
        "date": "10/Oct/2026:08:30:00.123",
        "frontend": "vs_shop",
        "backend": "pool_shop",
        "server": "10-20-3-11-8080",
        "tq": "0",
        "tw": "0",
        "tc": "1",
        "tr": "12",
        "tt": "13",
        "status": "200",
        "bytes": "512",
        "request_id": "6f5b0f7e-6e0a-4a4f-9d2b-2b6d2f9a1c33",
        "sni": '"shop.example.test"',
        "host": '"shop.example.test"',
        "upstream": "10.20.3.11",
        "upstream_port": "8080",
        "retries": "1",
    }


def test_the_controller_parser_reads_the_same_line() -> None:
    entry = parse_httplog(rendered_line())
    assert entry is not None
    assert (entry.frontend, entry.backend, entry.server) == (
        "vs_shop",
        "pool_shop",
        "10-20-3-11-8080",
    )
    assert (entry.status, entry.bytes, entry.duration_ms) == (200, 512, 13)
    assert (entry.method, entry.path) == ("GET", "/cart?x=1")
    assert entry.request_id == "6f5b0f7e-6e0a-4a4f-9d2b-2b6d2f9a1c33"
    assert (entry.sni, entry.host) == ("shop.example.test", "shop.example.test")
    assert (entry.upstream, entry.upstream_port, entry.retries) == ("10.20.3.11", 8080, 1)


@pytest.mark.parametrize("sni", ["-", '""'])
def test_a_request_without_sni_leaves_the_field_empty(sni: str) -> None:
    """Plain HTTP has no SNI: HAProxy logs ``-`` (or an empty quoted string) and the line still parses."""
    line = rendered_line().replace('sni="shop.example.test"', f"sni={sni}")
    assert vector_pattern().search(line) is not None
    entry = parse_httplog(line)
    assert entry is not None and entry.sni is None and entry.host == "shop.example.test"


def test_a_request_with_no_server_leaves_the_upstream_empty() -> None:
    """A 503 from a backend with no server: HAProxy logs ``<NOSRV>`` and dashes, and the line still parses."""
    line = (
        rendered_line()
        .replace("pool_shop/10-20-3-11-8080", "pool_shop/<NOSRV>")
        .replace("0/0/1/12/13", "0/-1/-1/-1/0")
        .replace("upstream=10.20.3.11", "upstream=-")
        .replace("upstream_port=8080", "upstream_port=-")
    )
    assert vector_pattern().search(line) is not None
    entry = parse_httplog(line)
    assert entry is not None
    assert (entry.server, entry.duration_ms) == ("<NOSRV>", 0)
    assert (entry.upstream, entry.upstream_port) == (None, None)


def test_a_default_httplog_line_still_parses() -> None:
    """An engine that has not been upgraded yet logs the default format: same fields, none of the new ones."""
    line = (
        "192.0.2.7:51514 [10/Oct/2026:08:30:00.123] vs_shop pool_web/web-1 0/0/1/12/13 200 512 "
        '- - ---- 1/1/0/0/0 0/0 "GET /cart?x=1 HTTP/1.1"'
    )
    parsed = vector_pattern().search(line)
    assert parsed is not None
    assert (parsed["frontend"], parsed["status"]) == ("vs_shop", "200")
    entry = parse_httplog(line)
    assert entry is not None and entry.request_id is None and entry.sni is None


def test_a_quoted_host_with_an_escaped_quote_does_not_split_the_line() -> None:
    """``%{+Q}`` escapes a hostile Host header, so the field boundary of the next tag survives."""
    line = rendered_line().replace('host="shop.example.test"', 'host="bad\\"host"')
    parsed = vector_pattern().search(line)
    assert parsed is not None
    assert parsed["host"] == '"bad\\"host"'
    assert parsed["upstream"] == "10.20.3.11"
    entry = parse_httplog(line)
    assert entry is not None and entry.host == 'bad"host'
