"""Request and response models of the controller cluster members API (ADR 0051)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from switchyard.api.schemas import ApiModel

PlatformName = Literal["proxmox", "esxi", "other"]


class ControllerMemberIn(ApiModel):
    """A controller VM to list."""

    name: str = Field(
        description="The Patroni member name (`PATRONI_NAME`). Status is matched on it."
    )
    address: str = Field(description="The VM's IP address or host name.")
    platform: PlatformName = Field(description="What the VM runs on.")
    host: str | None = Field(
        default=None, description="A label for the hypervisor host, for example `esxi-01`."
    )
    external: bool | None = Field(
        default=None,
        description="Not managed by this platform. Default: true unless `platform` is `proxmox`.",
    )
    nofailover: bool = Field(
        default=False,
        description="The member never becomes the primary (Patroni tag `nofailover`). Patroni's "
        "own report wins when it is reachable.",
    )
    patroni_url: str | None = Field(
        default=None,
        description="Patroni's REST API, for example `http://10.0.0.13:8008`. Status is read "
        "from the first listed member that answers.",
    )


class ControllerMemberPatch(ApiModel):
    """Fields to change; `name` is fixed. Send the `ETag` in `If-Match`."""

    address: str | None = None
    platform: PlatformName | None = None
    host: str | None = None
    external: bool | None = None
    nofailover: bool | None = None
    patroni_url: str | None = None


class MemberStatusRead(ApiModel):
    """What Patroni reports; every field is null when it could not be read."""

    database_role: str | None = Field(
        description="`leader`, `standby_leader`, `sync_standby` or `replica`."
    )
    database_state: str | None = Field(description="For example `running` or `streaming`.")
    lag_bytes: int | None = Field(description="Replication lag; null for the leader or unknown.")
    timeline: int | None
    nofailover: bool | None = Field(description="Patroni's `nofailover` tag.")


class ControllerMemberRead(ApiModel):
    """A controller cluster member."""

    id: UUID
    name: str
    address: str
    platform: PlatformName
    host: str | None
    external: bool = Field(description="Not managed by the platform (for example on ESXi).")
    nofailover: bool
    patroni_url: str | None
    created_by: str
    created_at: datetime
    version: int
    status: MemberStatusRead
    in_cluster: bool | None = Field(
        description="Whether Patroni lists it; null when no cluster view could be read."
    )


class ControllerMembersRead(ApiModel):
    """The members and the cluster view they were matched with."""

    members: list[ControllerMemberRead]
    cluster_reachable: bool | None = Field(
        description="Whether a Patroni answered; null when no member has a `patroni_url`."
    )
    cluster_error: str | None = Field(description="Why no Patroni answered.")
    leader: str | None = Field(description="The member that holds the primary role.")
