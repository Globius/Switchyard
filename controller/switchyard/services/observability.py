# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The controller's own Prometheus metrics and the scrape-target list for the SEs.

Both read the database on request (admin role, all tenants), so every API replica answers the same
and a metric survives a restart: the controller keeps no counters in memory.

- :func:`collect_metrics` and :func:`render_exposition` give ``GET /metrics`` (text format 0.0.4).
- :func:`discovery_targets` gives the Prometheus HTTP service-discovery list for the SE exporters,
  labelled ``tenant``, ``se_group`` and ``vs`` (ADR 0061).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import ip_interface
from uuid import UUID

from sqlalchemy import func, select

from switchyard import __version__
from switchyard.db import models as m
from switchyard.db.ops_models import ChangeSetRow, EngineStatusRow, JobRow, ServiceEngineRow
from switchyard.db.tenancy import admin_session
from switchyard.services.context import Databases

#: Default exporter ports on an SE. HAProxy's exporter is not the local health port (8404).
EXPORTERS: dict[str, int] = {"haproxy": 8405, "node": 9100}


@dataclass
class Family:
    """One metric family: a name, a type and its samples.

    Attributes:
        name: Metric name.
        kind: ``gauge``, ``counter`` or ``summary``.
        help: One-line description.
        samples: ``(suffix, labels, value)``; the suffix (``_sum``, ``_count``) is appended to
            ``name`` and is empty for plain samples.
    """

    name: str
    kind: str
    help: str
    samples: list[tuple[str, Mapping[str, str], float]] = field(default_factory=list)

    def add(self, value: float, suffix: str = "", **labels: str) -> None:
        """Append a sample."""
        self.samples.append((suffix, labels, value))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: float) -> str:
    return repr(int(value)) if float(value).is_integer() else repr(float(value))


def render_exposition(families: Iterable[Family]) -> str:
    r"""Render families in the Prometheus text exposition format (version 0.0.4).

    Example:
        >>> f = Family("sy_jobs", "gauge", "Jobs.")
        >>> f.add(3, kind="apply", state='queued "x"')
        >>> print(render_exposition([f]), end="")
        # HELP sy_jobs Jobs.
        # TYPE sy_jobs gauge
        sy_jobs{kind="apply",state="queued \"x\""} 3
    """
    lines: list[str] = []
    for fam in families:
        lines.append(f"# HELP {fam.name} {fam.help}")
        lines.append(f"# TYPE {fam.name} {fam.kind}")
        for suffix, labels, value in fam.samples:
            rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
            braces = f"{{{rendered}}}" if rendered else ""
            lines.append(f"{fam.name}{suffix}{braces} {_number(value)}")
    return "\n".join(lines) + "\n"


#: The ``tenant`` label of work that belongs to no tenant (housekeeping and system jobs).
SYSTEM_TENANT = "system"


@dataclass(frozen=True)
class EngineSample:
    """One engine's latest status poll."""

    tenant: UUID
    reachable: bool
    drifted: bool
    findings: int
    observed_at: datetime


@dataclass(frozen=True)
class ServiceSample:
    """One virtual service, for the per-service series."""

    tenant: UUID
    name: str
    protocol: str
    enabled: bool


@dataclass(frozen=True)
class MetricRows:
    """What :func:`collect_metrics` reads, so that :func:`metric_families` stays a pure function."""

    jobs: tuple[tuple[UUID | None, str, str, int], ...] = ()
    job_seconds: tuple[tuple[UUID | None, str, str, int, float], ...] = ()
    change_sets: tuple[tuple[UUID, str, int], ...] = ()
    engines: tuple[EngineSample, ...] = ()
    #: Tenants that own at least one engine row, whether or not it has been polled yet. Without
    #: this a tenant whose engines have never reported would expose no reachability series at all,
    #: which reads as "no engines" to an alerting rule.
    engine_tenants: tuple[UUID, ...] = ()
    services: tuple[ServiceSample, ...] = ()


def tenant_label(tenant: UUID | None) -> str:
    """The ``tenant`` label value: the tenant id, or :data:`SYSTEM_TENANT` for work without one."""
    return SYSTEM_TENANT if tenant is None else str(tenant)


def metric_families(rows: MetricRows, *, now: datetime, version: str) -> list[Family]:
    """The controller's families, every series labelled by tenant (and, per service, by name).

    Metrics: ``switchyard_info``, ``switchyard_jobs`` (by tenant, kind and state),
    ``switchyard_job_duration_seconds`` (a summary of finished jobs by tenant, kind and outcome),
    ``switchyard_change_sets`` (by tenant and state; ``failed`` are apply failures),
    ``switchyard_engines`` (by tenant and reachability), ``switchyard_engines_drifted``,
    ``switchyard_drift_findings``, ``switchyard_status_age_seconds`` (how stale the newest engine
    poll of that tenant is, so a dead poller can be alerted on) and ``switchyard_virtual_services``
    (one series of 1 per virtual service, so a tenant's series join to the objects it owns).

    **Cardinality argument.** Every label value is a point in a set whose size the configuration
    fixes; no label comes from request data:

    - ``tenant`` is the id of a ``tenant`` row (the id every counted row references through a
      foreign key), plus the single literal ``system`` for jobs that belong to no tenant. The
      dimension is the number of tenants.
    - ``kind``, ``state``, ``outcome``, ``protocol`` and ``enabled`` are enums of the domain
      models, so each adds a fixed number of values.
    - ``switchyard_info`` carries no tenant label: it is one sample per build, not tenant data.
    - ``service`` is a virtual service's own name, read from ``virtual_service``: one series per
      row, so the dimension is bounded by the tenant's virtual services, and deleting one drops
      its series. It is never a value a caller sends.
    - Per-request values (request id, host, SNI, client address, upstream) are deliberately absent:
      they belong to the access-log line, where Loki owns per-request cardinality (ADR 0052).

    A family therefore has at most ``tenants x |enum values|`` series, times the number of virtual
    services for the per-service family — the size of the configuration, not of the traffic.

    Example:
        >>> rows = MetricRows(services=(ServiceSample(UUID(int=7), "shop", "http", True),))
        >>> f = {x.name: x for x in metric_families(rows, now=datetime.now(UTC), version="1")}
        >>> f["switchyard_virtual_services"].samples[0][1]["service"]
        'shop'
    """
    info = Family("switchyard_info", "gauge", "Controller build information.")
    info.add(1, version=version)
    jobs = Family("switchyard_jobs", "gauge", "Jobs in the queue, by tenant, kind and state.")
    durations = Family(
        "switchyard_job_duration_seconds",
        "summary",
        "Run time of finished jobs, by tenant, kind and outcome (retention removes old rows).",
    )
    change_sets = Family("switchyard_change_sets", "gauge", "Change sets by tenant and state.")
    engines = Family("switchyard_engines", "gauge", "Enabled engines by tenant and reachability.")
    drifted = Family(
        "switchyard_engines_drifted", "gauge", "Engines whose running config drifted, by tenant."
    )
    findings = Family("switchyard_drift_findings", "gauge", "Drift findings, by tenant.")
    age = Family(
        "switchyard_status_age_seconds",
        "gauge",
        "Seconds since the newest engine status poll of that tenant.",
    )
    services = Family(
        "switchyard_virtual_services",
        "gauge",
        "Virtual services, one series per object (value 1), by tenant, name, protocol and state.",
    )

    for tenant, kind, state, count in sorted(
        rows.jobs, key=lambda r: (tenant_label(r[0]), r[1], r[2])
    ):
        jobs.add(count, tenant=tenant_label(tenant), kind=kind, state=state)
    for tenant, kind, state, count, total in sorted(
        rows.job_seconds, key=lambda r: (tenant_label(r[0]), r[1], r[2])
    ):
        durations.add(count, "_count", tenant=tenant_label(tenant), kind=kind, outcome=state)
        durations.add(float(total), "_sum", tenant=tenant_label(tenant), kind=kind, outcome=state)
    for tenant, state, count in sorted(rows.change_sets, key=lambda r: (str(r[0]), r[1])):
        change_sets.add(count, tenant=str(tenant), state=state)

    reachable: dict[str, int] = {}
    unreachable: dict[str, int] = {}
    per_engine_drift: dict[str, int] = {}
    per_engine_findings: dict[str, int] = {}
    newest: dict[str, datetime] = {}
    for engine in rows.engines:
        owner = str(engine.tenant)
        if engine.reachable:
            reachable[owner] = reachable.get(owner, 0) + 1
        else:
            unreachable[owner] = unreachable.get(owner, 0) + 1
        if engine.drifted:
            per_engine_drift[owner] = per_engine_drift.get(owner, 0) + 1
        per_engine_findings[owner] = per_engine_findings.get(owner, 0) + engine.findings
        newest[owner] = max(newest.get(owner, engine.observed_at), engine.observed_at)
    for owner, drifted_count in sorted(per_engine_drift.items()):
        drifted.add(drifted_count, tenant=owner)
    for owner, findings_count in sorted(per_engine_findings.items()):
        findings.add(findings_count, tenant=owner)
    for owner, observed_at in sorted(newest.items()):
        age.add(max(0.0, (now - observed_at).total_seconds()), tenant=owner)
    # Both reachability values for every tenant that has an engine, so a query does not have to
    # distinguish "no unreachable engine" from "no engine".
    owners = set(reachable) | set(unreachable) | {str(t) for t in rows.engine_tenants}
    for owner in sorted(owners):
        engines.add(reachable.get(owner, 0), tenant=owner, reachable="true")
        engines.add(unreachable.get(owner, 0), tenant=owner, reachable="false")

    for service in sorted(rows.services, key=lambda s: (str(s.tenant), s.name)):
        services.add(
            1,
            tenant=str(service.tenant),
            service=service.name,
            protocol=service.protocol,
            enabled="true" if service.enabled else "false",
        )
    return [info, jobs, durations, change_sets, engines, drifted, findings, age, services]


async def collect_metrics(db: Databases, *, now: datetime | None = None) -> list[Family]:
    """Read the metrics from the database and label them per tenant.

    Everything is grouped by ``tenant`` (the tenant id, as the telemetry contract uses) and the
    per-service series carry the virtual service's own name. See :func:`metric_families` for the
    families, the label sets and why they cannot grow without bound.

    Example:
        >>> print(render_exposition([f for f in metric_families(MetricRows(), now=datetime.now(UTC),
        ...     version="0.1.0") if f.name == "switchyard_info"]), end="")
        # HELP switchyard_info Controller build information.
        # TYPE switchyard_info gauge
        switchyard_info{version="0.1.0"} 1
    """
    now = now or datetime.now(UTC)
    async with admin_session(db.admin) as session:
        jobs = [
            (tenant, kind, state, int(count))
            for tenant, kind, state, count in (
                await session.execute(
                    select(JobRow.tenant_id, JobRow.kind, JobRow.state, func.count()).group_by(
                        JobRow.tenant_id, JobRow.kind, JobRow.state
                    )
                )
            ).all()
        ]
        seconds = func.extract("epoch", JobRow.finished_at - JobRow.started_at)
        job_seconds = [
            (tenant, kind, state, int(count), float(total))
            for tenant, kind, state, count, total in (
                await session.execute(
                    select(
                        JobRow.tenant_id,
                        JobRow.kind,
                        JobRow.state,
                        func.count(),
                        func.coalesce(func.sum(seconds), 0),
                    )
                    .where(JobRow.finished_at.is_not(None), JobRow.started_at.is_not(None))
                    .group_by(JobRow.tenant_id, JobRow.kind, JobRow.state)
                )
            ).all()
        ]
        change_sets = [
            (tenant, state, int(count))
            for tenant, state, count in (
                await session.execute(
                    select(ChangeSetRow.tenant_id, ChangeSetRow.state, func.count()).group_by(
                        ChangeSetRow.tenant_id, ChangeSetRow.state
                    )
                )
            ).all()
        ]
        engines = [
            EngineSample(
                tenant=tenant,
                reachable=bool(reachable),
                drifted=bool(drift),
                findings=len(drift),
                observed_at=observed_at,
            )
            for tenant, reachable, drift, observed_at in (
                await session.execute(
                    select(
                        EngineStatusRow.tenant_id,
                        EngineStatusRow.reachable,
                        EngineStatusRow.drift,
                        EngineStatusRow.observed_at,
                    )
                )
            ).all()
        ]
        services = [
            ServiceSample(tenant=tenant, name=name, protocol=str(protocol), enabled=bool(enabled))
            for tenant, name, protocol, enabled in (
                await session.execute(
                    select(
                        m.VirtualServiceRow.tenant_id,
                        m.VirtualServiceRow.name,
                        m.VirtualServiceRow.protocol,
                        m.VirtualServiceRow.enabled,
                    )
                )
            ).all()
        ]
        engine_tenants = tuple(
            tenant
            for (tenant,) in (
                await session.execute(select(ServiceEngineRow.tenant_id).distinct())
            ).all()
        )
    return metric_families(
        MetricRows(
            jobs=tuple(jobs),
            job_seconds=tuple(job_seconds),
            change_sets=tuple(change_sets),
            engines=tuple(engines),
            engine_tenants=engine_tenants,
            services=tuple(services),
        ),
        now=now,
        version=__version__,
    )


def _host(address: object) -> str:
    """A bare host for a target; IPv6 gets brackets."""
    ip = ip_interface(str(address)).ip
    return f"[{ip}]" if ip.version == 6 else str(ip)


async def discovery_targets(
    db: Databases, *, exporter: str = "haproxy", port: int | None = None
) -> list[dict[str, object]]:
    """Prometheus HTTP service-discovery groups, one per enabled engine with a management address.

    Labels: ``tenant`` (tenant id, the label the telemetry contract uses), ``tenant_name``,
    ``se_group``, ``engine`` and ``vs`` (the group's virtual service names between commas, for
    example ``,shop,www,``, so a relabel regex like ``.*,shop,.*`` can match).

    Raises:
        ValueError: If ``exporter`` is not one of :data:`EXPORTERS`.
    """
    if exporter not in EXPORTERS:
        raise ValueError(f"exporter must be one of: {', '.join(sorted(EXPORTERS))}")
    chosen = port if port is not None else EXPORTERS[exporter]
    async with admin_session(db.admin) as session:
        rows = (
            await session.execute(
                select(ServiceEngineRow, m.TenantRow.name, m.SeGroupRow.name)
                .join(m.TenantRow, m.TenantRow.id == ServiceEngineRow.tenant_id)
                .join(m.SeGroupRow, m.SeGroupRow.id == ServiceEngineRow.se_group_id)
                .where(ServiceEngineRow.state == "enabled", ServiceEngineRow.mgmt_ip.is_not(None))
                .order_by(m.TenantRow.name, m.SeGroupRow.name, ServiceEngineRow.name)
            )
        ).all()
        services: dict[object, list[str]] = {}
        for group_id, name in await session.execute(
            select(m.VirtualServiceRow.se_group_id, m.VirtualServiceRow.name).order_by(
                m.VirtualServiceRow.name
            )
        ):
            services.setdefault(group_id, []).append(name)
    return [
        {
            "targets": [f"{_host(engine.mgmt_ip)}:{chosen}"],
            "labels": {
                "tenant": str(engine.tenant_id),
                "tenant_name": tenant_name,
                "se_group": group_name,
                "engine": engine.name,
                "vs": "," + ",".join(services.get(engine.se_group_id, [])) + ",",
            },
        }
        for engine, tenant_name, group_name in rows
    ]
