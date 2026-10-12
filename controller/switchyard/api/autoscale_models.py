"""Response model of the autoscale status API (ADR 0194)."""

from datetime import datetime
from typing import Literal

from switchyard.api.schemas import ApiModel


class AutoscaleRead(ApiModel):
    """What the autoscaler last saw and did for one SE group."""

    se_group: str
    enabled: bool
    engines: int
    max_engines: int
    scale_out_at: int
    utilization: int | None
    decision: Literal[
        "off", "idle", "busy", "at_max", "cooldown", "blocked", "scaling", "scale_out"
    ]
    reason: str | None
    last_scaled_at: datetime | None
