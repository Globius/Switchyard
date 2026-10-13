# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The project's built-in signing keys (ADR 0205, 0206)."""

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from switchyard.services.release_feed import PROJECT_KEYS


def test_every_project_key_is_a_raw_ed25519_public_key() -> None:
    assert "release-2026" in PROJECT_KEYS
    for key_id, value in PROJECT_KEYS.items():
        raw = base64.b64decode(value, validate=True)
        assert len(raw) == 32, key_id
        Ed25519PublicKey.from_public_bytes(raw)


def test_the_nightly_key_is_never_a_project_key() -> None:
    assert not any("nightly" in key_id for key_id in PROJECT_KEYS)
