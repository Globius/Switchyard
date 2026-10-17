# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests: every certificate a crt-list names reaches the engine; failed applies fail."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from switchyard.adapters.secrets import MemorySecrets
from switchyard.domain.loader import snapshot_from_mapping
from switchyard.jobs.handlers import ChangeSetFailedError, build_handlers
from switchyard.pipeline.apply import ApplyReport
from switchyard.pipeline.bundles import BundleError, engine_files
from switchyard.render.haproxy import render_haproxy
from switchyard.render.routing import EngineSlot
from switchyard.services.changesets import APPLY_JOB
from tests.unit.domain.test_protection import GATE, protection, with_sets

PEM = "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\nPRIVATE KEY\n"


async def test_a_certificate_named_by_a_crt_list_is_bundled() -> None:
    snap = snapshot_from_mapping(with_sets(protection(captcha=GATE)))
    group = snap.se_groups[0]
    slot = EngineSlot("se-1", 0, "192.0.2.5")
    refs = {c.name: c.secret_ref for c in snap.certificates}
    secrets = MemorySecrets(dict.fromkeys(refs.values(), PEM) | {str(GATE["secret_ref"]): "s\n"})
    rendered = dict(render_haproxy(snap, group.id).files)
    (name,) = refs
    rendered["certs/extra.crtlist"] = f"/etc/switchyard/certs/{name}.pem a.example.test\n"
    files = await engine_files(snap, group, slot, secrets, None, rendered)
    assert f"certs/{name}.pem" in files
    rendered["certs/extra.crtlist"] = "/etc/switchyard/certs/nope.pem a.example.test\n"
    with pytest.raises(BundleError, match=r"nope.pem"):
        await engine_files(snap, group, slot, secrets, None, rendered)


async def test_a_failed_change_set_fails_the_job() -> None:
    cs = uuid4()
    reports = iter([ApplyReport(False, cs, error="canary failed"), ApplyReport(True, cs, 3)])

    async def run(*_a: Any, **_k: Any) -> ApplyReport:
        return next(reports)

    db = SimpleNamespace(admin=None)
    handler = build_handlers(db, SimpleNamespace(run=run), None)[APPLY_JOB]  # type: ignore[arg-type]
    job = SimpleNamespace(
        id=uuid4(), request_id=None, payload={"tenant_id": str(uuid4()), "change_set_id": str(cs)}
    )
    with pytest.raises(ChangeSetFailedError, match="canary failed"):
        await handler(job)  # type: ignore[arg-type]
    assert (await handler(job))["ok"] is True  # type: ignore[arg-type,index]
