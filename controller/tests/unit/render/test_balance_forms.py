# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The ``balance`` line of each hashing algorithm, in HAProxy's own spelling.

HAProxy is asymmetric here: ``hdr`` takes its key in parentheses, ``url_param`` takes it as a
separate word. Emitting ``balance url_param(<name>)`` is accepted by nothing: ``haproxy -c`` 3.2
answers "balance only supports ... 'hdr(name)' ..." and the push fails at the canary.
"""

import uuid

import pytest

from switchyard.domain import models as dm
from switchyard.render.haproxy import _Renderer


def _pool(algorithm: dm.Algorithm, key: str | None) -> dm.Pool:
    """A minimal pool with the given algorithm and hash key."""
    return dm.Pool(
        tenant_id=uuid.UUID(int=2),
        id=uuid.UUID(int=1),
        name="shards",
        algorithm=algorithm,
        hash_key=key,
    )


@pytest.mark.parametrize(
    ("algorithm", "key", "expected"),
    [
        (dm.Algorithm.URI, None, "balance uri"),
        (dm.Algorithm.HDR, "X-Shard-Key", "balance hdr(X-Shard-Key)"),
        (dm.Algorithm.URL_PARAM, "tenant_id", "balance url_param tenant_id"),
        (dm.Algorithm.ROUNDROBIN, None, "balance roundrobin"),
    ],
)
def test_balance_line_matches_haproxy_syntax(
    algorithm: dm.Algorithm, key: str | None, expected: str
) -> None:
    """Each algorithm renders the form HAProxy 3.2 accepts (``haproxy -c`` verified)."""
    assert _Renderer._balance(_pool(algorithm, key)) == expected
