"""Request and response models of the alerting API (ADR 0120)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from switchyard.api.schemas import ApiModel


class AlertTargetIn(ApiModel):
    """A place alerts go."""

    name: str = Field(description="Lowercase letters, digits, `-` and `_`; unique in the tenant.")
    kind: Literal["webhook", "chat", "email"]
    config: dict[str, Any] = Field(
        description="`webhook`: `{url}`. `chat`: `{url, format: slack|teams|mattermost}`. "
        "`email`: `{to: [address, ...]}`. URLs are https and name no private address."
    )
    secret_ref: str | None = Field(
        default=None,
        description="Webhooks only: an OpenBao path (`kv/...`) whose value signs each body "
        "(`X-Switchyard-Signature: sha256=<hmac>`). The secret itself is never stored here.",
    )
    enabled: bool = True


class AlertTargetPatch(ApiModel):
    """Fields of a target that can change; omitted fields stay as they are."""

    config: dict[str, Any] | None = None
    secret_ref: str | None = None
    enabled: bool | None = None


class AlertTargetRead(ApiModel):
    """A target."""

    id: UUID
    name: str
    kind: str
    config: dict[str, Any]
    secret_ref: str | None
    enabled: bool
    version: int
    created_by: str
    created_at: datetime


class AlertTargetTest(ApiModel):
    """The outcome of a test message."""

    ok: bool
    error: str | None = Field(default=None, description="Why delivery failed, when it did.")


class AlertRuleIn(ApiModel):
    """What to watch and who to tell."""

    name: str
    kind: Literal["event", "metric", "cert_expiry"]
    spec: dict[str, Any] = Field(
        description="`event`: `{actions: [audit action, ...]}`. `metric`: `{metric, op: gt|lt, "
        "threshold, for_minutes, scope?}` with `metric` from `GET /alert-catalog`. "
        "`cert_expiry`: `{days: [30, 14, 7]}`."
    )
    targets: list[str] = Field(description="Names of targets in the same tenant (1 to 10).")
    enabled: bool = True


class AlertRulePatch(ApiModel):
    """Fields of a rule that can change; omitted fields stay as they are."""

    spec: dict[str, Any] | None = None
    targets: list[str] | None = None
    enabled: bool | None = None


class AlertRuleRead(ApiModel):
    """A rule."""

    id: UUID
    name: str
    kind: str
    spec: dict[str, Any]
    targets: list[str]
    enabled: bool
    version: int
    created_by: str
    created_at: datetime


class AlertFiring(ApiModel):
    """One watched thing that is firing (or waiting out `for_minutes`)."""

    rule: str
    key: str
    state: Literal["firing", "pending"]
    since: datetime
    details: dict[str, Any]
    fires_at: datetime | None = Field(
        default=None,
        description="`pending` only: when the alert fires if the value stays past the threshold "
        "(`since` plus the rule's `for_minutes`).",
    )


class AlertDeliveryRead(ApiModel):
    """One attempt to tell one target."""

    rule: str | None
    target: str
    key: str
    kind: Literal["firing", "resolved", "test"]
    status: Literal["sent", "failed"]
    error: str | None
    at: datetime


class AlertStatus(ApiModel):
    """What is firing now and the latest deliveries."""

    firing: list[AlertFiring]
    deliveries: list[AlertDeliveryRead]


class AlertMetricInfo(ApiModel):
    """A series a metric rule can watch."""

    name: str
    label: str
    unit: str
    scope: Literal["vs", "engine"]


class AlertCatalog(ApiModel):
    """What the rule and target forms offer."""

    metrics: list[AlertMetricInfo]
    event_actions: list[str]
    chat_formats: list[str]
    email_configured: bool = Field(description="False when email targets cannot send.")
