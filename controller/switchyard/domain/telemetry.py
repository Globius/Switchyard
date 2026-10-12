"""Metric and access-log shapes of a virtual service (what the UI charts and tails).

Plain data; the adapter in :mod:`switchyard.adapters.telemetry` fills it from Prometheus and Loki.
A value is ``None`` when the backend has no data for it, which is not the same as zero.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    """Base class: strict about unknown fields."""

    model_config = ConfigDict(extra="forbid")


class MetricPoint(Model):
    """One sample of a series."""

    t: datetime
    v: float


class MetricSummary(Model):
    """The latest value of each headline metric over the window."""

    rps: float | None = Field(default=None, description="HTTP/TCP requests per second.")
    p95_ms: float | None = Field(default=None, description="95th percentile response time.")
    error_5xx_ratio: float | None = Field(
        default=None, description="Share of responses that are 5xx, 0 to 1."
    )
    active_connections: float | None = Field(default=None, description="Current sessions.")


class ServiceMetrics(Model):
    """Time series and headline numbers of one virtual service."""

    window_s: int
    step_s: int
    summary: MetricSummary
    rps: list[MetricPoint]
    p95_ms: list[MetricPoint]
    error_5xx_ratio: list[MetricPoint]
    active_connections: list[MetricPoint]


class EngineSummary(Model):
    """The latest value of each headline metric of one service engine."""

    cpu_percent: float | None = Field(
        default=None, description="CPU used, as a percentage of all vCPUs (0 to 100)."
    )
    vcpus: float | None = Field(default=None, description="Number of vCPUs the engine sees.")
    memory_used_bytes: float | None = Field(default=None, description="Memory in use.")
    memory_total_bytes: float | None = Field(default=None, description="Memory the engine has.")
    haproxy_sessions: float | None = Field(
        default=None, description="Current HAProxy sessions on this engine."
    )
    traffic_in_bps: float | None = Field(
        default=None, description="Bits per second received by HAProxy on this engine."
    )


class EngineMetrics(Model):
    """Time series and headline numbers of one service engine."""

    window_s: int
    step_s: int
    summary: EngineSummary
    cpu_percent: list[MetricPoint]
    memory_used_bytes: list[MetricPoint]
    haproxy_sessions: list[MetricPoint]
    traffic_in_bps: list[MetricPoint]


class AccessLogEntry(Model):
    """One parsed HAProxy HTTP log line."""

    at: datetime
    client_ip: str
    frontend: str
    backend: str
    server: str
    status: int
    bytes: int
    duration_ms: int = Field(description="Total time of the request (HAProxy `Tt`).")
    method: str | None = None
    path: str | None = None
    waf: bool = Field(
        default=False,
        description="The web application firewall refused or dropped this request "
        "(the line carries its verdict).",
    )
    waf_action: str | None = Field(
        default=None, description="The WAF verdict on the line: `deny` or `drop`."
    )
    raw: str = Field(description="The log line as stored.")
