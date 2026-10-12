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

import logging
import os
import socket
from datetime import timedelta
from logging.handlers import SysLogHandler
from pathlib import Path
from urllib.parse import urlparse

from switchyard.adapters.acme import AcmeIssuer, new_account_key
from switchyard.adapters.agent import BundleSigner
from switchyard.adapters.cloudflare import Cloudflare, HttpCloudflare
from switchyard.adapters.git import GitLabMirror, GitMirror
from switchyard.adapters.grafana import HttpGrafana
from switchyard.adapters.pki import OpenBaoPki
from switchyard.adapters.proxmox import HttpProxmox
from switchyard.adapters.sandbox import DockerSandbox
from switchyard.adapters.secrets import EnvSecrets, OpenBaoSecrets, SecretsReader
from switchyard.adapters.telemetry import HttpTelemetry, Telemetry
from switchyard.config import database_url
from switchyard.db.tenancy import ADMIN_ROLE, APP_ROLE, make_engine
from switchyard.domain.validators import configure_secret_policy
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
from switchyard.services.grafana import GrafanaConfig, GrafanaService
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
    if (
        bundle_signer() is not None
        and not ca
        and os.environ.get("SWITCHYARD_INSECURE_AGENT") != "1"
    ):
        raise RuntimeError(
            "SWITCHYARD_BUNDLE_KEY_FILE is set but SWITCHYARD_DATAPLANE_CA is not: agent bundles "
            "(which carry private keys) would travel without TLS. Set the CA, or "
            "SWITCHYARD_INSECURE_AGENT=1 for a throwaway lab."
        )
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


def telemetry() -> Telemetry | None:
    """Prometheus (and Loki) for metrics and logs, if ``SWITCHYARD_PROMETHEUS_URL`` is set.

    ``SWITCHYARD_LOKI_URL`` adds access logs and the 95th percentile.
    """
    prometheus = os.environ.get("SWITCHYARD_PROMETHEUS_URL")
    if not prometheus:
        return None
    return HttpTelemetry(prometheus, os.environ.get("SWITCHYARD_LOKI_URL"))


def grafana(db: Databases) -> GrafanaService | None:
    """The embedded Grafana (ADR 0080), if every setting it needs is present.

    ``SWITCHYARD_GRAFANA_INTERNAL_URL`` (Grafana, ``http://host:3000``, reachable from the
    controller only), ``SWITCHYARD_GRAFANA_ADMIN_PASSWORD`` (and optionally
    ``SWITCHYARD_GRAFANA_ADMIN_USER``, default ``admin``), ``SWITCHYARD_GRAFANA_DS_SECRET``
    (at least 32 characters; derives the per-tenant data source tokens),
    ``SWITCHYARD_GRAFANA_DS_URL`` (where *Grafana* reaches this controller's ``/internal/ds``) and
    ``SWITCHYARD_PROMETHEUS_URL``; ``SWITCHYARD_LOKI_URL`` adds logs. Without them the UI keeps
    the plain link from ``SWITCHYARD_GRAFANA_URL``.
    """
    names = (
        "SWITCHYARD_GRAFANA_INTERNAL_URL",
        "SWITCHYARD_GRAFANA_ADMIN_PASSWORD",
        "SWITCHYARD_GRAFANA_DS_SECRET",
        "SWITCHYARD_GRAFANA_DS_URL",
        "SWITCHYARD_PROMETHEUS_URL",
    )
    values = [os.environ.get(n, "").strip() for n in names]
    if not all(values):
        return None
    internal, password, ds_secret, ds_url, prometheus = values
    config = GrafanaConfig(
        internal_url=internal.rstrip("/"),
        admin_user=os.environ.get("SWITCHYARD_GRAFANA_ADMIN_USER", "admin"),
        admin_password=password,
        ds_secret=ds_secret,
        datasource_url=ds_url.rstrip("/"),
        prometheus_url=prometheus.rstrip("/"),
        loki_url=os.environ.get("SWITCHYARD_LOKI_URL") or None,
    )
    admin = HttpGrafana(config.internal_url, config.admin_user, config.admin_password)
    return GrafanaService(db, config, admin)


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
    client = proxmox_client()
    return InfraService(db, client) if client else None


def audit_syslog_target(value: str) -> tuple[str | tuple[str, int], socket.SocketKind]:
    """Parse ``SWITCHYARD_AUDIT_SYSLOG`` into a ``SysLogHandler`` address and socket type.

    Forms: ``udp://host[:514]``, ``tcp://host[:514]`` and ``unix:///dev/log``.

    Example:
        >>> audit_syslog_target("udp://logs.example.test")[0]
        ('logs.example.test', 514)
        >>> audit_syslog_target("unix:///dev/log")[0]
        '/dev/log'
        >>> audit_syslog_target("http://x")
        Traceback (most recent call last):
            ...
        ValueError: SWITCHYARD_AUDIT_SYSLOG must be udp://host[:port], tcp://host[:port] or unix:///path
    """
    parsed = urlparse(value)
    message = "SWITCHYARD_AUDIT_SYSLOG must be udp://host[:port], tcp://host[:port] or unix:///path"
    if parsed.scheme == "unix" and parsed.path:
        return parsed.path, socket.SOCK_DGRAM
    if parsed.scheme in {"udp", "tcp"} and parsed.hostname:
        kind = socket.SOCK_DGRAM if parsed.scheme == "udp" else socket.SOCK_STREAM
        return (parsed.hostname, parsed.port or 514), kind
    raise ValueError(message)


def setup_audit_syslog() -> bool:
    """Send audit events to syslog when ``SWITCHYARD_AUDIT_SYSLOG`` is set. Returns whether it did.

    Audit events are always written to the database; this adds a copy as one JSON line per event
    on the ``switchyard.audit`` logger, facility ``local0``, so a central log host keeps them even
    if the database is lost.
    """
    value = os.environ.get("SWITCHYARD_AUDIT_SYSLOG", "").strip()
    if not value:
        return False
    address, kind = audit_syslog_target(value)
    handler = SysLogHandler(address=address, facility=SysLogHandler.LOG_LOCAL0, socktype=kind)
    handler.setFormatter(logging.Formatter("switchyard-audit: %(message)s"))
    audit = logging.getLogger("switchyard.audit")
    audit.addHandler(handler)
    audit.setLevel(logging.INFO)
    return True


def configure_secret_policy_from_env() -> None:
    """Apply ``SWITCHYARD_SHARED_SECRET_PREFIXES`` and ``SWITCHYARD_SECRET_POLICY`` (ADR 0072).

    ``SWITCHYARD_SHARED_SECRET_PREFIXES`` is a comma-separated list of paths every tenant may name
    besides its own ``kv/tenants/<tenant>/`` (each ending in ``/``);
    ``SWITCHYARD_SECRET_POLICY=open`` allows any other path (labs only).
    """
    raw = os.environ.get("SWITCHYARD_SHARED_SECRET_PREFIXES", "")
    prefixes = tuple(p.strip() for p in raw.split(",") if p.strip())
    configure_secret_policy(prefixes, open=os.environ.get("SWITCHYARD_SECRET_POLICY") == "open")


def grafana_url() -> str | None:
    """The Grafana base URL from ``SWITCHYARD_GRAFANA_URL``, if it is an http(s) URL.

    The UI links to it, so anything else (a ``javascript:`` URL, a path) is ignored.

    Example:
        >>> import os
        >>> os.environ["SWITCHYARD_GRAFANA_URL"] = "https://grafana.example.test/"
        >>> grafana_url()
        'https://grafana.example.test'
        >>> os.environ["SWITCHYARD_GRAFANA_URL"] = "javascript:alert(1)"
        >>> grafana_url() is None
        True
        >>> del os.environ["SWITCHYARD_GRAFANA_URL"]
    """
    value = os.environ.get("SWITCHYARD_GRAFANA_URL", "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        return None
    return value.rstrip("/")


def proxmox_client() -> HttpProxmox | None:
    """The Proxmox client, if ``SWITCHYARD_PVE_URL``, the token id and its secret are set."""
    url = os.environ.get("SWITCHYARD_PVE_URL")
    token_id = os.environ.get("SWITCHYARD_PVE_TOKEN_ID")
    if not url or not token_id:
        return None
    ref = os.environ.get("SWITCHYARD_PVE_TOKEN_REF", "kv/proxmox/token")
    secret = os.environ.get(EnvSecrets.variable_name(ref))
    if not secret:
        return None
    ca = os.environ.get("SWITCHYARD_PVE_CA")
    return HttpProxmox(url, token_id, secret, verify=ca or True)


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
