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


async def collect_metrics(db: Databases, *, now: datetime | None = None) -> list[Family]:
    """Read the metrics from the database.

    Metrics: ``switchyard_info``, ``switchyard_jobs`` (by kind and state),
    ``switchyard_job_duration_seconds`` (a summary of finished jobs by kind and outcome),
    ``switchyard_change_sets`` (by state; ``failed`` are apply failures),
    ``switchyard_engines`` (by reachability), ``switchyard_engines_drifted``,
    ``switchyard_drift_findings`` and ``switchyard_status_age_seconds`` (how stale the newest
    engine poll is, so a dead poller can be alerted on).
    """
    now = now or datetime.now(UTC)
    info = Family("switchyard_info", "gauge", "Controller build information.")
    info.add(1, version=__version__)
    jobs = Family("switchyard_jobs", "gauge", "Jobs in the queue, by kind and state.")
    durations = Family(
        "switchyard_job_duration_seconds",
        "summary",
        "Run time of finished jobs, by kind and outcome (jobs older than the retention are gone).",
    )
    change_sets = Family("switchyard_change_sets", "gauge", "Change sets by state.")
    engines = Family("switchyard_engines", "gauge", "Enabled engines by reachability.")
    drifted = Family("switchyard_engines_drifted", "gauge", "Engines whose running config drifted.")
    findings = Family("switchyard_drift_findings", "gauge", "Drift findings across all engines.")
    age = Family(
        "switchyard_status_age_seconds", "gauge", "Seconds since the newest engine status poll."
    )
    async with admin_session(db.admin) as session:
        for kind, state, count in await session.execute(
            select(JobRow.kind, JobRow.state, func.count()).group_by(JobRow.kind, JobRow.state)
        ):
            jobs.add(count, kind=kind, state=state)
        seconds = func.extract("epoch", JobRow.finished_at - JobRow.started_at)
        for kind, state, count, total in await session.execute(
            select(JobRow.kind, JobRow.state, func.count(), func.coalesce(func.sum(seconds), 0))
            .where(JobRow.finished_at.is_not(None), JobRow.started_at.is_not(None))
            .group_by(JobRow.kind, JobRow.state)
        ):
            durations.add(count, "_count", kind=kind, outcome=state)
            durations.add(float(total), "_sum", kind=kind, outcome=state)
        for state, count in await session.execute(
            select(ChangeSetRow.state, func.count()).group_by(ChangeSetRow.state)
        ):
            change_sets.add(count, state=state)
        status = (await session.execute(select(EngineStatusRow))).scalars().all()
    reachable = sum(1 for s in status if s.reachable)
    engines.add(reachable, reachable="true")
    engines.add(len(status) - reachable, reachable="false")
    drifted.add(sum(1 for s in status if s.drift))
    findings.add(sum(len(s.drift) for s in status))
    if status:
        age.add(max(0.0, (now - max(s.observed_at for s in status)).total_seconds()))
    return [info, jobs, durations, change_sets, engines, drifted, findings, age]


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
