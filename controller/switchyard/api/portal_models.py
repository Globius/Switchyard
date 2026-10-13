# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Request and response models of the tenant portal API: quota requests (ADR 0199)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from switchyard.api.schemas import ApiModel

QuotaRequestStateName = Literal["pending", "approved", "denied", "withdrawn"]


class QuotaRequestIn(ApiModel):
    """A request for a higher limit."""

    resource: str = Field(
        description="The quota key, an object type such as `virtual_service`, or "
        "`stick_table_entries`. The tenant must already have a limit for it."
    )
    requested_limit: int = Field(
        ge=1, description="The limit the tenant wants. It must be higher than the current one."
    )
    reason: str = Field(max_length=500, description="Why the tenant needs it.")


class QuotaDecisionIn(ApiModel):
    """The decision on a quota request."""

    granted_limit: int | None = Field(
        default=None,
        ge=1,
        description="Approve only: the new limit, between the current limit and the requested "
        "one. Default: the requested limit.",
    )
    note: str | None = Field(
        default=None,
        max_length=500,
        description="Shown to the tenant. Required when the request is denied.",
    )


class QuotaRequestRead(ApiModel):
    """A quota request."""

    id: UUID
    tenant_id: UUID
    tenant: str
    resource: str
    current_limit: int = Field(description="The tenant's limit when the request was filed.")
    requested_limit: int
    reason: str
    state: QuotaRequestStateName
    requested_by: str
    requested_at: datetime
    granted_limit: int | None = Field(description="Set when the request was approved.")
    decided_by: str | None = Field(description="The decider, or the requester who withdrew it.")
    decided_at: datetime | None
    decision_note: str | None
