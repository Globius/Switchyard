# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CrowdSec policy sets: model, attachment rules, SPOE and bouncer files, key handling (ADR 0215)."""

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.adapters.secrets import MemorySecrets
from switchyard.db.crowdsec_config import StoredBouncer
from switchyard.domain import models as dm
from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.snapshot import validate_snapshot
from switchyard.pipeline.bundles import BundleError, engine_files
from switchyard.render.crowdsec import (
    BOUNCER_PATH,
    LOG_FEED_PATH,
    BouncerSettings,
    render_bouncer_files,
    render_log_feed_files,
)
from switchyard.render.escape import RenderError
from switchyard.render.haproxy import render_haproxy
from switchyard.render.routing import EngineSlot
from tests.factories import BASE
from tests.unit.domain.test_pool_health_parity import TCP_SERVICE, data

KEY = "k3y-s3cret-bouncer-value"
SETTINGS = BouncerSettings("https://crowdsec.example.test:8080", KEY, ("crowdsec", "CAPI"))


def crowdsec(**extra: Any) -> dict[str, Any]:
    return {"id": "ps-cs", "tenant_id": "t-acme", "name": "cs", "kind": "crowdsec", **extra}


def with_policies(*policies: dict[str, Any]) -> dict[str, Any]:
    d = copy.deepcopy(BASE)
    d["policy_sets"] = list(policies)
    d["virtual_services"][0]["policy_set_ids"] = [p["id"] for p in policies]
    return d


def problems(d: dict[str, Any]) -> list[tuple[str, str]]:
    return [(p.path, p.code) for p in validate_snapshot(snapshot_from_mapping(d))]


def haproxy(d: dict[str, Any]) -> dict[str, str]:
    snap = snapshot_from_mapping(d)
    return dict(render_haproxy(snap, snap.se_groups[0].id).files)


def bouncer(d: dict[str, Any], settings: BouncerSettings | None = SETTINGS) -> dict[str, str]:
    snap = snapshot_from_mapping(d)
    return render_bouncer_files(snap, snap.se_groups[0].id, settings)


def test_defaults_start_in_detect_mode_failing_open() -> None:
    p = dm.CrowdSecPolicySet.model_validate(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "tenant_id": "00000000-0000-0000-0000-000000000002",
            "name": "c",
        }
    )
    assert (p.mode, p.fail_mode, p.timeout_ms) == ("detect", "open", 100)


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": "challenge"},
        {"fail_mode": "maybe"},
        {"timeout_ms": 5},
        {"allow_cidrs": ["not-a-network"]},
        {"allow_cidrs": ["192.0.2.0/24", "192.0.2.0/24"]},
        {"excluded_paths": ["/a b"]},
        {"excluded_paths": ["/a", "/a"]},
    ],
)
def test_bad_settings_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        snapshot_from_mapping(with_policies(crowdsec(**bad)))


def test_one_crowdsec_policy_per_service() -> None:
    d = with_policies(crowdsec(), crowdsec(id="ps-two", name="two"))
    assert problems(d) == [("virtual_services.web.policy_set_ids", "too_many_crowdsec_policies")]


def test_tcp_services_cannot_carry_policy_sets() -> None:
    d = data({}, {**TCP_SERVICE, "policy_set_ids": ["ps-cs"]})
    d["policy_sets"] = [crowdsec()]
    with pytest.raises(ValidationError):
        snapshot_from_mapping(d)


def test_detect_mode_asks_the_bouncer_and_logs_but_never_blocks() -> None:
    files = haproxy(with_policies(crowdsec()))
    cfg = files["haproxy/haproxy.cfg"]
    assert "filter spoe engine crowdsec config /etc/switchyard/haproxy/lists/crowdsec.spoe" in cfg
    assert "http-request send-spoe-group crowdsec crowdsec-http-no-body" in cfg
    assert "capture var(txn.crowdsec.remediation)" in cfg
    assert "-m str ban" not in cfg and "txn.crowdsec.error" not in cfg
    assert "backend sy_crowdsec_spoa" in cfg and "server crowdsec 127.0.0.1:9001" in cfg
    assert "spoe-agent crowdsec-agent" in files["haproxy/lists/crowdsec.spoe"]


def test_block_mode_refuses_bans_and_fail_closed_refuses_on_error() -> None:
    cfg = haproxy(with_policies(crowdsec(mode="block", fail_mode="closed")))["haproxy/haproxy.cfg"]
    assert "deny_status 403 if { var(txn.crowdsec.remediation) -m str ban }" in cfg
    assert "deny_status 503 if { var(txn.crowdsec.error) -m int gt 0 }" in cfg


def test_allowlisted_networks_and_paths_skip_the_check() -> None:
    cfg = haproxy(
        with_policies(
            crowdsec(allow_cidrs=["198.51.100.0/24", "192.0.2.7/32"], excluded_paths=["/healthz"])
        )
    )["haproxy/haproxy.cfg"]
    assert (
        "send-spoe-group crowdsec crowdsec-http-no-body"
        " if !{ src 192.0.2.7/32 198.51.100.0/24 } !{ path_beg /healthz }"
    ) in cfg


def test_the_refusals_honour_the_allowlist_too() -> None:
    cfg = haproxy(
        with_policies(
            crowdsec(
                mode="block",
                fail_mode="closed",
                allow_cidrs=["198.51.100.0/24"],
                excluded_paths=["/healthz"],
            )
        )
    )["haproxy/haproxy.cfg"]
    skip = "!{ src 198.51.100.0/24 } !{ path_beg /healthz }"
    assert f"deny_status 403 if {{ var(txn.crowdsec.remediation) -m str ban }} {skip}" in cfg
    assert f"deny_status 503 if {{ var(txn.crowdsec.error) -m int gt 0 }} {skip}" in cfg
    assert f"deny_status 503 if !{{ var(txn.crowdsec.remediation) -m found }} {skip}" in cfg


def test_nothing_is_rendered_without_a_crowdsec_policy() -> None:
    files = haproxy(copy.deepcopy(BASE))
    assert "haproxy/lists/crowdsec.spoe" not in files
    assert "sy_crowdsec_spoa" not in files["haproxy/haproxy.cfg"]
    assert bouncer(copy.deepcopy(BASE), None) == {}


def test_disabled_services_do_not_start_the_bouncer() -> None:
    d = with_policies(crowdsec())
    d["virtual_services"][0]["enabled"] = False
    assert bouncer(d) == {}


def test_the_bouncer_file_carries_the_settings_as_json_strings() -> None:
    text = bouncer(with_policies(crowdsec()))[BOUNCER_PATH]
    assert 'api_url: "https://crowdsec.example.test:8080"' in text
    assert f'api_key: "{KEY}"' in text
    assert 'origins: ["CAPI", "crowdsec"]' in text
    assert 'listen_tcp: "127.0.0.1:9001"' in text
    assert "fallback_remediation: allow" in text and "insecure_skip_verify: false" in text


def test_a_key_that_tries_to_break_out_of_its_line_stays_a_string() -> None:
    evil = BouncerSettings("https://x.example.test", 'a"\nlisten_tcp: "0.0.0.0:1', ())
    text = bouncer(with_policies(crowdsec()), evil)[BOUNCER_PATH]
    assert sum(1 for line in text.splitlines() if line.startswith("listen_tcp:")) == 1
    assert 'api_key: "a\\"\\nlisten_tcp: \\"0.0.0.0:1"' in text


def test_a_policy_without_settings_cannot_render() -> None:
    with pytest.raises(RenderError, match="not configured"):
        bouncer(with_policies(crowdsec()), None)


CERT = {"kv/tenants/acme/certs/wild-example-test": "BEGIN CERTIFICATE PRIVATE KEY"}


async def loader_with(key_ref: str = "", env_key: str = "") -> Any:
    async def load() -> StoredBouncer:
        return StoredBouncer(
            "https://crowdsec.example.test:8080", key_ref, env_key, ("crowdsec",), ""
        )

    return load


async def test_the_key_comes_from_the_secrets_backend_and_travels_in_the_bundle_only() -> None:
    snap = snapshot_from_mapping(with_policies(crowdsec()))
    group = snap.se_groups[0]
    slot = EngineSlot("se-1", 0, "192.0.2.5")
    store = MemorySecrets({"kv/system/crowdsec/bouncer-key": KEY + "\n", **CERT})
    files = await engine_files(
        snap, group, slot, store, await loader_with("kv/system/crowdsec/bouncer-key")
    )
    assert f'api_key: "{KEY}"' in files[BOUNCER_PATH]
    rendered = render_haproxy(snap, group.id).files
    assert KEY not in "".join(rendered.values())


async def test_the_environment_key_is_used_when_there_is_no_reference() -> None:
    snap = snapshot_from_mapping(with_policies(crowdsec()))
    files = await engine_files(
        snap,
        snap.se_groups[0],
        EngineSlot("se-1", 0, "192.0.2.5"),
        MemorySecrets(CERT),
        await loader_with("", "env-key"),
    )
    assert 'api_key: "env-key"' in files[BOUNCER_PATH]


async def test_a_missing_or_unusable_key_or_configuration_stops_the_bundle() -> None:
    snap = snapshot_from_mapping(with_policies(crowdsec()))
    group, slot = snap.se_groups[0], EngineSlot("se-1", 0, "192.0.2.5")
    store = MemorySecrets(CERT)
    with pytest.raises(BundleError, match="not configured"):
        await engine_files(snap, group, slot, store, None)
    with pytest.raises(BundleError, match="was not found"):
        await engine_files(snap, group, slot, store, await loader_with("kv/system/missing"))
    with pytest.raises(BundleError, match="one non-empty line"):
        await engine_files(snap, group, slot, store, await loader_with("", ""))
    both = MemorySecrets({"kv/system/two": "a\nb", **CERT})
    with pytest.raises(BundleError, match="one non-empty line"):
        await engine_files(snap, group, slot, both, await loader_with("kv/system/two"))


async def test_services_without_a_policy_never_load_the_settings() -> None:
    snap = snapshot_from_mapping(copy.deepcopy(BASE))

    async def boom() -> None:
        raise AssertionError("the settings must not be read")

    files = await engine_files(
        snap, snap.se_groups[0], EngineSlot("se-1", 0, "192.0.2.5"), MemorySecrets(CERT), boom
    )
    assert BOUNCER_PATH not in files


def test_the_sample_used_by_the_image_check_is_what_the_renderer_writes() -> None:
    sample = Path(__file__).parents[4] / "se-image" / "test" / "crowdsec-config.yaml"
    text = bouncer(
        with_policies(crowdsec()),
        BouncerSettings(
            "http://crowdsec.example.test:8080", "sample-key-not-a-secret", ("crowdsec",)
        ),
    )[BOUNCER_PATH]
    assert sample.read_text("utf-8") == text


# -- the access log feed -----------------------------------------------------------------------------

FEED = BouncerSettings("https://crowdsec.example.test:8080", KEY, (), "crowdsec.example.test:514")


def two_services(**extra: Any) -> dict[str, Any]:
    d = with_policies(crowdsec(**extra))
    other = copy.deepcopy(d["virtual_services"][0])
    other.update(id="vs-two", name="two", vip="192.0.2.31", certificate_ids=[], routes=[])
    other["ports"] = [{"port": 80}]
    other["default_pool_id"] = "pool-web"
    other["policy_set_ids"] = []
    d["virtual_services"].append(other)
    return d


def feed(d: dict[str, Any], settings: BouncerSettings | None = FEED) -> dict[str, str]:
    snap = snapshot_from_mapping(d)
    return render_log_feed_files(snap, snap.se_groups[0].id, settings)


def test_logs_are_not_shared_unless_the_policy_asks() -> None:
    assert feed(with_policies(crowdsec())) == {}
    assert feed(copy.deepcopy(BASE), None) == {}


def test_the_feed_selects_only_the_services_that_share() -> None:
    text = feed(two_services(share_logs=True))[LOG_FEED_PATH]
    assert 'condition: ".frontend == \\"vs_web\\""' in text
    assert "vs_two" not in text
    assert 'address: "crowdsec.example.test:514"' in text
    assert "mode: udp" in text and "inputs: [parse]" in text


def test_the_feed_wraps_each_line_as_rfc3164_with_the_haproxy_program() -> None:
    text = feed(with_policies(crowdsec(share_logs=True)))[LOG_FEED_PATH]
    assert '"<134>" + stamp + " " + host + " haproxy[1]: "' in text
    assert 'format: "%b %e %H:%M:%S"' in text


def test_sharing_without_a_log_address_cannot_render() -> None:
    d = with_policies(crowdsec(share_logs=True))
    with pytest.raises(RenderError, match="no log address"):
        feed(d, BouncerSettings("https://x.example.test", KEY, ()))
    with pytest.raises(RenderError, match="no log address"):
        feed(d, None)


def test_the_feed_never_carries_the_bouncer_key() -> None:
    assert KEY not in feed(with_policies(crowdsec(share_logs=True)))[LOG_FEED_PATH]


async def test_the_bundle_carries_the_feed_next_to_the_bouncer_file() -> None:
    snap = snapshot_from_mapping(with_policies(crowdsec(share_logs=True)))

    async def load() -> StoredBouncer:
        return StoredBouncer(
            "https://crowdsec.example.test:8080", "", KEY, (), "crowdsec.example.test:514"
        )

    files = await engine_files(
        snap,
        snap.se_groups[0],
        EngineSlot("se-1", 0, "192.0.2.5"),
        MemorySecrets(CERT),
        load,
    )
    assert BOUNCER_PATH in files and LOG_FEED_PATH in files


# -- one key per engine ------------------------------------------------------------------------------


async def per_engine_loader() -> StoredBouncer:
    return StoredBouncer(
        "https://crowdsec.example.test:8080",
        "kv/system/crowdsec/bouncer-key",
        "",
        (),
        "",
        per_engine_keys=True,
    )


async def test_each_engine_gets_its_own_key_and_the_shared_key_is_not_used() -> None:
    snap = snapshot_from_mapping(with_policies(crowdsec()))
    store = MemorySecrets(
        {
            "kv/system/crowdsec/bouncer-key": "shared-key-value",
            "kv/system/crowdsec/bouncer-keys/edge-1": "key-for-edge-1",
            "kv/system/crowdsec/bouncer-keys/edge-2": "key-for-edge-2",
            **CERT,
        }
    )
    got = {}
    for index, name in enumerate(("edge-1", "edge-2")):
        files = await engine_files(
            snap,
            snap.se_groups[0],
            EngineSlot(name, index, "192.0.2.5"),
            store,
            per_engine_loader,
        )
        got[name] = files[BOUNCER_PATH]
    assert 'api_key: "key-for-edge-1"' in got["edge-1"]
    assert 'api_key: "key-for-edge-2"' in got["edge-2"]
    assert "shared-key-value" not in got["edge-1"] + got["edge-2"]


async def test_an_engine_without_its_own_key_stops_the_bundle_with_the_fix_in_the_message() -> None:
    snap = snapshot_from_mapping(with_policies(crowdsec()))
    store = MemorySecrets({"kv/system/crowdsec/bouncer-key": "shared-key-value", **CERT})
    with pytest.raises(
        BundleError, match=r"bouncer-keys/edge-9 was not found.*register-engine.sh edge-9"
    ):
        await engine_files(
            snap, snap.se_groups[0], EngineSlot("edge-9", 0, "192.0.2.5"), store, per_engine_loader
        )


async def test_one_key_per_engine_is_off_unless_asked_for() -> None:
    snap = snapshot_from_mapping(with_policies(crowdsec()))
    store = MemorySecrets({"kv/system/crowdsec/bouncer-key": "shared-key-value", **CERT})
    files = await engine_files(
        snap,
        snap.se_groups[0],
        EngineSlot("edge-1", 0, "192.0.2.5"),
        store,
        await loader_with("kv/system/crowdsec/bouncer-key"),
    )
    assert 'api_key: "shared-key-value"' in files[BOUNCER_PATH]
