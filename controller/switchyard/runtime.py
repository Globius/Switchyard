"""Process wiring for the API and worker entry points, configured from the environment.

Variables: ``SWITCHYARD_DATABASE_URL`` (required in production), ``SWITCHYARD_DATAPLANE_CA``,
``SWITCHYARD_DATAPLANE_CERT`` and ``SWITCHYARD_DATAPLANE_KEY`` (mTLS to the engines),
``SWITCHYARD_GITLAB_URL``, ``SWITCHYARD_GITLAB_PROJECT`` and ``SWITCHYARD_GITLAB_TOKEN`` (Git
mirror, optional), ``SWITCHYARD_SANDBOX=docker`` (validate with ``haproxy -c`` in a container).
Secrets: with ``SWITCHYARD_OPENBAO_ADDR``, ``SWITCHYARD_OPENBAO_ROLE_ID`` and
``SWITCHYARD_OPENBAO_SECRET_ID`` (optionally ``SWITCHYARD_OPENBAO_CA``) references are read from
OpenBao KV v2; otherwise from ``SWITCHYARD_SECRET_*`` variables (see ``EnvSecrets``), which cannot
store new certificates. Proxmox: ``SWITCHYARD_PVE_URL``, ``SWITCHYARD_PVE_TOKEN_ID`` and the token
secret. Certificates: ``SWITCHYARD_CLOUDFLARE_TOKEN`` (DNS-01, Origin CA, IP ranges) and, for ACME,
``SWITCHYARD_ACME_DIRECTORY``, ``SWITCHYARD_ACME_EMAIL`` and ``SWITCHYARD_ACME_ACCOUNT_KEY_FILE``.
Login: ``SWITCHYARD_PUBLIC_URL`` (external base URL, for redirect URIs and Secure cookies),
``SWITCHYARD_BREAK_GLASS=1`` (allow the local administrator). Agents:
``SWITCHYARD_BUNDLE_KEY_FILE`` (Ed25519 key that signs bundles) and, for enrollment, OpenBao PKI
(``SWITCHYARD_PKI_MOUNT``, ``SWITCHYARD_PKI_ROLE``).
"""

import os
from datetime import timedelta
from pathlib import Path

from switchyard.adapters.acme import AcmeIssuer, new_account_key
from switchyard.adapters.agent import BundleSigner
from switchyard.adapters.cloudflare import Cloudflare, HttpCloudflare
from switchyard.adapters.git import GitLabMirror, GitMirror
from switchyard.adapters.pki import OpenBaoPki
from switchyard.adapters.proxmox import HttpProxmox
from switchyard.adapters.sandbox import DockerSandbox
from switchyard.adapters.secrets import EnvSecrets, OpenBaoSecrets, SecretsReader
from switchyard.config import database_url
from switchyard.db.tenancy import ADMIN_ROLE, APP_ROLE, make_engine
from switchyard.jobs.handlers import POLL_STATUS_JOB, build_handlers
from switchyard.jobs.scheduler import Scheduler
from switchyard.jobs.worker import Worker
from switchyard.pipeline.apply import ApplyPipeline
from switchyard.pipeline.engines import DataPlaneTls, DbEngineDirectory
from switchyard.services.certificates import (
    RENEW_CERTS_JOB,
    SYNC_CLOUDFLARE_IPS_JOB,
    CertificateService,
)
from switchyard.services.changesets import ChangeSetService
from switchyard.services.context import Databases
from switchyard.services.enrollment import EnrollmentService
from switchyard.services.gc import GC_JOB, GarbageCollector
from switchyard.services.infra import InfraService
from switchyard.services.oidc import OidcClient
from switchyard.services.sessions import SessionService
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
    return DbEngineDirectory(db, secrets_backend(), tls, signer=bundle_signer())


def bundle_signer() -> BundleSigner | None:
    """The Ed25519 key that signs agent bundles, from ``SWITCHYARD_BUNDLE_KEY_FILE`` (PKCS#8 PEM).

    Without it engines are treated as having no agent.
    """
    path = os.environ.get("SWITCHYARD_BUNDLE_KEY_FILE")
    return BundleSigner.from_pem(Path(path).read_text()) if path else None


def enrollment(db: Databases, secrets: SecretsReader | None = None) -> EnrollmentService | None:
    """Agent enrollment, if OpenBao PKI and a bundle signing key are configured.

    The PKI mount and role default to ``pki`` and ``agents`` (``SWITCHYARD_PKI_MOUNT`` and
    ``SWITCHYARD_PKI_ROLE``).
    """
    store = secrets or secrets_backend()
    signer = bundle_signer()
    if not isinstance(store, OpenBaoSecrets) or signer is None:
        return None
    pki = OpenBaoPki(
        store,
        mount=os.environ.get("SWITCHYARD_PKI_MOUNT", "pki"),
        role=os.environ.get("SWITCHYARD_PKI_ROLE", "agents"),
    )
    return EnrollmentService(db, pki, signer.public_b64())


def sessions(db: Databases, secrets: SecretsReader | None = None) -> SessionService:
    """Browser login. ``SWITCHYARD_BREAK_GLASS=1`` switches the local administrator login on."""
    return SessionService(
        db,
        OidcClient(),
        secrets or secrets_backend(),
        break_glass_enabled=os.environ.get("SWITCHYARD_BREAK_GLASS") == "1",
    )


def secrets_backend() -> SecretsReader:
    """OpenBao when configured, environment variables otherwise."""
    addr = os.environ.get("SWITCHYARD_OPENBAO_ADDR")
    role = os.environ.get("SWITCHYARD_OPENBAO_ROLE_ID")
    secret = os.environ.get("SWITCHYARD_OPENBAO_SECRET_ID")
    if addr and role and secret:
        return OpenBaoSecrets(
            addr, role, secret, verify=os.environ.get("SWITCHYARD_OPENBAO_CA") or True
        )
    return EnvSecrets()


def cloudflare() -> Cloudflare | None:
    """The Cloudflare client, if ``SWITCHYARD_CLOUDFLARE_TOKEN`` is set."""
    token = os.environ.get("SWITCHYARD_CLOUDFLARE_TOKEN")
    return HttpCloudflare(token) if token else None


def acme(dns: Cloudflare | None) -> AcmeIssuer | None:
    """The ACME issuer, if a directory, an account key file and DNS are configured.

    A missing account key file is created (mode 0600) on first use.
    """
    directory_url = os.environ.get("SWITCHYARD_ACME_DIRECTORY")
    key_file = os.environ.get("SWITCHYARD_ACME_ACCOUNT_KEY_FILE")
    if not directory_url or not key_file or dns is None:
        return None
    path = Path(key_file)
    if not path.exists():
        path.write_text(new_account_key())
        path.chmod(0o600)
    return AcmeIssuer(
        directory_url, path.read_text(), dns, email=os.environ.get("SWITCHYARD_ACME_EMAIL")
    )


def certificates(db: Databases, secrets: SecretsReader | None = None) -> CertificateService | None:
    """Certificate service, if the secrets backend can store key material (OpenBao)."""
    store = secrets or secrets_backend()
    if not isinstance(store, OpenBaoSecrets):
        return None
    dns = cloudflare()
    return CertificateService(db, store, ChangeSetService(db), cloudflare=dns, acme=acme(dns))


def git_mirror() -> GitMirror | None:
    """The GitLab mirror, if configured."""
    url = os.environ.get("SWITCHYARD_GITLAB_URL")
    project = os.environ.get("SWITCHYARD_GITLAB_PROJECT")
    token = os.environ.get("SWITCHYARD_GITLAB_TOKEN")
    return GitLabMirror(url, project, token) if url and project and token else None


def infra_service(db: Databases, secrets: SecretsReader) -> InfraService | None:
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
    secrets = secrets_backend()
    pipeline = ApplyPipeline(db, engines, sandbox=sandbox, secrets=secrets)
    status = StatusService(db, engines, secrets)
    certs = certificates(db, secrets)
    handlers = build_handlers(
        db, pipeline, git_mirror(), status, infra_service(db, secrets), certs, GarbageCollector(db)
    )
    schedule = {POLL_STATUS_JOB: timedelta(seconds=10), GC_JOB: timedelta(hours=1)}
    if certs is not None:
        schedule[RENEW_CERTS_JOB] = timedelta(days=1)
        if os.environ.get("SWITCHYARD_CLOUDFLARE_TOKEN"):
            schedule[SYNC_CLOUDFLARE_IPS_JOB] = timedelta(days=1)
    scheduler = Scheduler(db.admin, schedule)
    return Worker(db.admin, handlers, worker_id=worker_id), scheduler
