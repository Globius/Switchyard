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
from switchyard.services.gc import GC_JOB
from switchyard.services.infra import APPLY_SDN_JOB, DEPLOY_SE_GROUP_JOB, RETIRE_ENGINE_JOB


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
        EVALUATE_ALERTS_JOB,
    }
    assert kinds == {k.value for k in JobKind}
