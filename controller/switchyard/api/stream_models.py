# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Payloads of the server-sent events of ``GET /tenants/{t}/status/stream``.

The stream is documented here because OpenAPI cannot type an event stream: each event name maps
to one of these models, listed under ``x-sse-events`` of the operation.
"""

from switchyard.api.job_models import Job
from switchyard.api.schemas import (
    EngineStatusRead,
    RevisionSummary,
    VirtualServiceStatusRead,
)

#: Event name -> (payload model, whether the payload is an array, when it is sent).
EVENTS: dict[str, tuple[type, bool, str]] = {
    "status": (
        EngineStatusRead,
        True,
        "The status of every engine, at the start and whenever any of it changes.",
    ),
    "vs_state": (
        VirtualServiceStatusRead,
        True,
        "State and health of every virtual service, at the start and whenever any changes.",
    ),
    "job_progress": (
        Job,
        False,
        "One job whenever its state or a stage changes (unfinished jobs are sent at the start).",
    ),
    "revision_applied": (
        RevisionSummary,
        False,
        "A new revision became the desired state (not sent for the revision at the start).",
    ),
}
