# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the signed release feed (ADR 0205)."""

import copy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from switchyard.domain.releases import (
    FeedError,
    ReleaseFeed,
    latest,
    public_key_b64,
    sign_feed,
    upgrade_path,
    verify_feed,
)

KEY = Ed25519PrivateKey.generate()
TRUSTED = {"k1": public_key_b64(KEY)}
PAYLOAD = {
    "schema": 1,
    "releases": [
        {
            "version": "0.2.0",
            "date": "2026-10-01",
            "notes": {"breaking": ["x"]},
            "upgrade_from": ["0.1.0"],
        },
        {"version": "0.3.0", "date": "2026-10-20", "upgrade_from": ["0.2.0"]},
        {"version": "0.4.0-rc1", "channel": "candidate", "date": "2026-10-25"},
    ],
}


def test_a_signed_feed_verifies() -> None:
    feed = verify_feed(sign_feed(PAYLOAD, KEY, "k1"), TRUSTED)
    assert [r.version for r in feed.releases] == ["0.2.0", "0.3.0", "0.4.0-rc1"]


def test_tampering_unknown_keys_and_other_signers_are_refused() -> None:
    doc = sign_feed(PAYLOAD, KEY, "k1")
    tampered = copy.deepcopy(doc)
    tampered["payload"]["releases"][0]["version"] = "9.9.9"
    with pytest.raises(FeedError, match="does not verify"):
        verify_feed(tampered, TRUSTED)
    with pytest.raises(FeedError, match="not trusted"):
        verify_feed(doc, {})
    other = sign_feed(PAYLOAD, Ed25519PrivateKey.generate(), "k1")
    with pytest.raises(FeedError, match="does not verify"):
        verify_feed(other, TRUSTED)


@pytest.mark.parametrize("doc", [None, [], {}, {"payload": 1, "signature": "x", "key_id": "k1"}])
def test_malformed_documents_are_refused(doc: object) -> None:
    with pytest.raises(FeedError):
        verify_feed(doc, TRUSTED)


def test_a_signed_but_invalid_payload_is_refused() -> None:
    bad = sign_feed({"schema": 1, "releases": [{"version": "x", "date": "today"}]}, KEY, "k1")
    with pytest.raises(FeedError, match="not a valid release feed"):
        verify_feed(bad, TRUSTED)
    insecure = {
        "schema": 1,
        "releases": [
            {
                "version": "1.0.0",
                "date": "2026-01-01",
                "bundle": {"url": "http://plain", "sha256": "0" * 64, "size_bytes": 1},
            }
        ],
    }
    with pytest.raises(FeedError):
        verify_feed(sign_feed(insecure, KEY, "k1"), TRUSTED)


def test_latest_respects_the_channel() -> None:
    feed = ReleaseFeed.model_validate(PAYLOAD)
    assert latest(feed).version == "0.3.0"  # type: ignore[union-attr]
    assert latest(feed, "candidate").version == "0.4.0-rc1"  # type: ignore[union-attr]
    assert latest(ReleaseFeed.model_validate({"schema": 1, "releases": []})) is None


def test_upgrade_path_follows_the_documented_chain() -> None:
    feed = ReleaseFeed.model_validate(PAYLOAD)
    assert upgrade_path(feed, "0.1.0", "0.3.0") == ["0.2.0", "0.3.0"]
    assert upgrade_path(feed, "0.2.0", "0.3.0") == ["0.3.0"]
    assert upgrade_path(feed, "0.3.0", "0.3.0") == []
    assert upgrade_path(feed, "dev", "0.3.0") == []
    assert upgrade_path(feed, "0.0.5", "0.3.0") == ["0.1.0", "0.2.0", "0.3.0"]


def test_the_nightly_channel_sees_everything_and_orders_by_precedence() -> None:
    feed = ReleaseFeed.model_validate(
        {
            "schema": 1,
            "releases": [
                {"version": "0.2.0", "date": "2026-10-20"},
                {"version": "0.3.0-nightly.20261013", "channel": "nightly", "date": "2026-10-13"},
                {"version": "0.3.0-nightly.20261014", "channel": "nightly", "date": "2026-10-14"},
                {"version": "0.3.0-rc1", "channel": "candidate", "date": "2026-10-12"},
            ],
        }
    )
    assert latest(feed).version == "0.2.0"  # type: ignore[union-attr]
    assert latest(feed, "candidate").version == "0.3.0-rc1"  # type: ignore[union-attr]
    # By semantic-version precedence nightly < rc < release, so an rc outranks the nightlies before it.
    assert latest(feed, "nightly").version == "0.3.0-rc1"  # type: ignore[union-attr]
    only_nightly = ReleaseFeed.model_validate(
        {"schema": 1, "releases": feed.model_dump(mode="json")["releases"][:3]}
    )
    assert latest(only_nightly, "nightly").version == "0.3.0-nightly.20261014"  # type: ignore[union-attr]
