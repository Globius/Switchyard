# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The published ``JobKind`` enum covers every job the worker can run."""

from switchyard.api.job_models import JobKind
from switchyard.jobs.handlers import GIT_MIRROR_JOB, POLL_STATUS_JOB
from switchyard.services.alerts import EVALUATE_ALERTS_JOB
from switchyard.services.certificates import (
    ISSUE_CERT_JOB,
    RENEW_CERTS_JOB,
    SYNC_CLOUDFLARE_IPS_JOB,
)
from switchyard.services.changesets import APPLY_JOB, REAPPLY_JOB
from switchyard.services.controller_settings import CONTROLLER_TICK_JOB
from switchyard.services.discovery import DISCOVER_POOLS_JOB
from switchyard.services.gc import GC_JOB
from switchyard.services.gslb import PROBE_GSLB_JOB, SYNC_GSLB_JOB
from switchyard.services.infra import APPLY_SDN_JOB, DEPLOY_SE_GROUP_JOB, RETIRE_ENGINE_JOB
from switchyard.services.iplists import SYNC_IP_LISTS_JOB
from switchyard.services.jwks_sync import SYNC_JWKS_JOB
from switchyard.services.kubernetes import KUBERNETES_SYNC_JOB


def test_every_job_kind_is_published() -> None:
    kinds = {
        APPLY_JOB,
        REAPPLY_JOB,
        ISSUE_CERT_JOB,
        RENEW_CERTS_JOB,
        SYNC_CLOUDFLARE_IPS_JOB,
        APPLY_SDN_JOB,
        RETIRE_ENGINE_JOB,
        DEPLOY_SE_GROUP_JOB,
        GIT_MIRROR_JOB,
        POLL_STATUS_JOB,
        GC_JOB,
        CONTROLLER_TICK_JOB,
        KUBERNETES_SYNC_JOB,
        DISCOVER_POOLS_JOB,
        SYNC_IP_LISTS_JOB,
        SYNC_JWKS_JOB,
        EVALUATE_ALERTS_JOB,
        SYNC_GSLB_JOB,
        PROBE_GSLB_JOB,
    }
    assert kinds == {k.value for k in JobKind}
