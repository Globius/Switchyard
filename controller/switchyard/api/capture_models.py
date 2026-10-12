# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Request and response models of the packet capture API (ADR 0194)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from switchyard.api.schemas import ApiModel


class CaptureIn(ApiModel):
    """What to capture on an engine. The agent builds the filter from these fields."""

    interface: str = Field(
        default="any", description="`any`, or the name of an interface of the engine (`eth0`)."
    )
    protocol: Literal["", "tcp", "udp", "icmp"] = Field(
        default="", description="Only this protocol; empty captures every protocol."
    )
    host: str = Field(default="", description="Only packets to or from this IP address.")
    port: int = Field(default=0, description="Only this port (tcp or udp), 0 for any.")
    duration_s: int = Field(default=10, description="Stop after this many seconds (1 to 60).")
    max_packets: int = Field(default=1000, description="Stop after this many packets (1 to 20000).")
    snaplen: int = Field(
        default=0, description="Bytes kept per packet (64 to 262144); 0 keeps whole packets."
    )


class CaptureRead(ApiModel):
    """A packet capture and how it went (the pcap itself is downloaded separately)."""

    id: UUID
    engine: str
    requested_by: str
    spec: CaptureIn
    state: Literal["queued", "running", "done", "failed"]
    packets: int | None
    size_bytes: int | None
    truncated: bool = Field(description="The size limit (16 MiB) ended the capture early.")
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    expires_at: datetime = Field(description="The capture is deleted at this time.")
