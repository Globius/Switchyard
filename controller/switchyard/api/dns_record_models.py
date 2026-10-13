# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Response models of the DNS address record API (ADR 0193)."""

from datetime import datetime
from typing import Literal

from switchyard.api.schemas import ApiModel


class DnsRecordRead(ApiModel):
    """An address record the controller publishes for a virtual service."""

    hostname: str
    type: Literal["A", "AAAA"]
    content: str
    ttl: int
    proxied: bool
    state: Literal["pending", "published", "error"]
    virtual_service: str | None
    last_error: str | None
    updated_at: datetime


class DnsRecordSyncResult(ApiModel):
    """What a sync did."""

    written: int
    deleted: int
    failed: int
