"""Process wiring for the API and worker entry points, configured from the environment.

Variables: ``SWITCHYARD_DATABASE_URL`` (required in production), ``SWITCHYARD_DATAPLANE_CA``,
``SWITCHYARD_DATAPLANE_CERT`` and ``SWITCHYARD_DATAPLANE_KEY`` (mTLS to the engines),
``SWITCHYARD_GITLAB_URL``, ``SWITCHYARD_GITLAB_PROJECT`` and ``SWITCHYARD_GITLAB_TOKEN`` (Git
mirror, optional), ``SWITCHYARD_SANDBOX=docker`` (validate with ``haproxy -c`` in a container).
Engine credentials come from ``SWITCHYARD_SECRET_*`` variables (see ``EnvSecrets``).
"""

import os
from pathlib import Path

from switchyard.adapters.git import GitLabMirror, GitMirror
from switchyard.adapters.sandbox import DockerSandbox
from switchyard.adapters.secrets import EnvSecrets
from switchyard.config import database_url
from switchyard.db.tenancy import ADMIN_ROLE, APP_ROLE, make_engine
from switchyard.jobs.handlers import build_handlers
from switchyard.jobs.worker import Worker
from switchyard.pipeline.apply import ApplyPipeline
from switchyard.pipeline.engines import DataPlaneTls, DbEngineDirectory
from switchyard.services.context import Databases


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


def worker(db: Databases, worker_id: str) -> Worker:
    """A worker with the apply and Git-mirror handlers."""
    sandbox = DockerSandbox() if os.environ.get("SWITCHYARD_SANDBOX") == "docker" else None
    pipeline = ApplyPipeline(db, directory(db), sandbox=sandbox)
    return Worker(db.admin, build_handlers(db, pipeline, git_mirror()), worker_id=worker_id)
