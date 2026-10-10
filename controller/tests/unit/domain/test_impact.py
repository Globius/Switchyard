"""Impact: which services a draft reaches."""

import uuid

from switchyard.domain.changes import Action, ChangeOp, ObjectType, apply_ops
from switchyard.domain.impact import impact
from switchyard.domain.loader import symbolic_uuid
from tests.factories import base_snapshot

TENANT = uuid.UUID(symbolic_uuid("t-acme"))
POOL = uuid.UUID(symbolic_uuid("pool-web"))
VS = uuid.UUID(symbolic_uuid("vs-web"))


def test_a_pool_change_reaches_the_services_that_use_it() -> None:
    base = base_snapshot()
    pool = next(p for p in base.pools if p.id == POOL)
    draft = apply_ops(
        base,
        [
            ChangeOp(
                object_type=ObjectType.POOL,
                object_id=POOL,
                action=Action.UPDATE,
                body={"algorithm": "source"},
                expected_version=pool.version,
            )
        ],
        tenant_id=TENANT,
    )
    got = impact(base, draft)
    vs = next(v for v in base.virtual_services if v.id == VS)
    assert [s[0] for s in got.services] == [VS]
    assert got.vips == (str(vs.vip),)
    assert got.se_group_ids == (vs.se_group_id,)


def test_an_unrelated_change_reaches_nothing() -> None:
    base = base_snapshot()
    draft = apply_ops(base, [], tenant_id=TENANT)
    got = impact(base, draft)
    assert got.services == () and got.vips == () and got.hostnames == ()
