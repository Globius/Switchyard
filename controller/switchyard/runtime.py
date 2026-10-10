"""Process wiring for the API and worker entry points, configured from the environment.

Variables: ``SWITCHYARD_DATABASE_URL`` (required in production), ``SWITCHYARD_DATAPLANE_CA``,
``SWITCHYARD_DATAPLANE_CERT`` and ``SWITCHYARD_DATAPLANE_KEY`` (mTLS to the engines),
``SWITCHYARD_GITLAB_URL``, ``SWITCHYARD_GITLAB_PROJECT`` and ``SWITCHYARD_GITLAB_TOKEN`` (Git
mirror, optional), ``SWITCHYARD_SANDBOX=docker`` (validate with ``haproxy -c`` in a container).
Engine credentials come from ``SWITCHYARD_SECRET_*`` variables (see ``EnvSecrets``).
"""

import os
from datetime import timedelta
from pathlib import Path

from switchyard.adapters.git import GitLabMirror, GitMirror
from switchyard.adapters.proxmox import HttpProxmox
from switchyard.adapters.sandbox import DockerSandbox
from switchyard.adapters.secrets import EnvSecrets
from switchyard.config import database_url
from switchyard.db.tenancy import ADMIN_ROLE, APP_ROLE, make_engine
from switchyard.jobs.handlers import POLL_STATUS_JOB, build_handlers
from switchyard.jobs.scheduler import Scheduler
from switchyard.jobs.worker import Worker
from switchyard.pipeline.apply import ApplyPipeline
from switchyard.pipeline.engines import DataPlaneTls, DbEngineDirectory
from switchyard.services.context import Databases
from switchyard.services.infra import InfraService
from switchyard.services.status import StatusService


def databases() -> Databases:
    """Open the application and admin engines."""
    url = database_url()
    return Databases(app=make_engine(url, role=APP_ROLE), admin=make_engine(url, role=ADMIN_ROLE))


def directory(db: Databases) -> DbEngineDirectory:
    """Engine directory with mTLS settings from the environment."""
    ca = os.environ.get("SWITCHYARD_DATAPLANE_CA")
    cert = os.environ.get("SWITCHYARD_DATAPLANE_CERT")
    key = os.environ.get("SWITCHYARD_DATAPLANE_KEY")
    tls = DataPlaneTls(
        ca_file=Path(ca) if ca else None,
        client_cert=(Path(cert), Path(key)) if cert and key else None,
    )
    return DbEngineDirectory(db, EnvSecrets(), tls)


def git_mirror() -> GitMirror | None:
    """The GitLab mirror, if configured."""
    url = os.environ.get("SWITCHYARD_GITLAB_URL")
    project = os.environ.get("SWITCHYARD_GITLAB_PROJECT")
    token = os.environ.get("SWITCHYARD_GITLAB_TOKEN")
    return GitLabMirror(url, project, token) if url and project and token else None


def infra_service(db: Databases, secrets: EnvSecrets) -> InfraService | None:
    """The Proxmox-backed infrastructure jobs, if ``SWITCHYARD_PVE_URL`` is configured.

    Also needs ``SWITCHYARD_PVE_TOKEN_ID`` and the token secret under the secret reference
    ``SWITCHYARD_PVE_TOKEN_REF`` (default ``kv/proxmox/token``); ``SWITCHYARD_PVE_CA`` is optional.
    """
    url = os.environ.get("SWITCHYARD_PVE_URL")
    token_id = os.environ.get("SWITCHYARD_PVE_TOKEN_ID")
    if not url or not token_id:
        return None
    ref = os.environ.get("SWITCHYARD_PVE_TOKEN_REF", "kv/proxmox/token")
    secret = os.environ.get(EnvSecrets.variable_name(ref))
    if not secret:
        return None
    ca = os.environ.get("SWITCHYARD_PVE_CA")
    return InfraService(db, HttpProxmox(url, token_id, secret, verify=ca or True))


def worker(db: Databases, worker_id: str) -> tuple[Worker, Scheduler]:
    """A worker (apply, Git mirror, status polling) and the scheduler that feeds it."""
    sandbox = DockerSandbox() if os.environ.get("SWITCHYARD_SANDBOX") == "docker" else None
    engines = directory(db)
    secrets = EnvSecrets()
    pipeline = ApplyPipeline(db, engines, sandbox=sandbox, secrets=secrets)
    status = StatusService(db, engines, secrets)
    handlers = build_handlers(db, pipeline, git_mirror(), status, infra_service(db, secrets))
    scheduler = Scheduler(db.admin, {POLL_STATUS_JOB: timedelta(seconds=10)})
    return Worker(db.admin, handlers, worker_id=worker_id), scheduler
