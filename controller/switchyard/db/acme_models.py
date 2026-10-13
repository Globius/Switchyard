# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tables for certificate authorities (ACME) and DNS credentials (ADR 0079).

``acme_provider`` is system-wide (a system administrator manages it; it has no tenant).
``dns_provider`` belongs to a tenant and has row-level security like every tenant table. Neither
stores a secret: the account key, the EAB key and the DNS credentials are in the secrets backend and
these rows hold the references.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from switchyard.db.base import Base, EntityMixin, TenantOwnedMixin, tenant_unique


class AcmeProviderRow(EntityMixin, Base):
    """``acme_provider``: an ACME certificate authority and the account the controller uses."""

    __tablename__ = "acme_provider"
    __table_args__ = (
        UniqueConstraint("name"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("auth_mode IN ('none', 'eab', 'oauth')", name="auth_mode"),
        Index(
            "uq_acme_provider_one_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    preset: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'custom'"))
    directory_url: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    #: ``none``, ``eab`` (static External Account Binding) or ``oauth`` (bearer token).
    auth_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'none'"))
    #: OAuth settings without the secret: token_url, client_id, scope, audience, bearer_on_acme,
    #: eab_url.
    oauth: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    oauth_secret_ref: Mapped[str | None] = mapped_column(Text)
    eab_kid: Mapped[str | None] = mapped_column(Text)
    eab_hmac_ref: Mapped[str | None] = mapped_column(Text)
    account_key_ref: Mapped[str] = mapped_column(Text, nullable=False)
    #: PEM of extra trusted root certificates for a private CA (public data).
    ca_bundle: Mapped[str | None] = mapped_column(Text)
    terms_agreed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    account_registered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class DnsProviderRow(TenantOwnedMixin, Base):
    """``dns_provider``: a tenant's DNS credential for publishing DNS-01 records."""

    __tablename__ = "dns_provider"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        CheckConstraint("version >= 1", name="version"),
    )

    type: Mapped[str] = mapped_column(Text, nullable=False)
    zones: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class CertificateRenewalRow(EntityMixin, Base):
    """``certificate_renewal``: one issuance or renewal attempt of a certificate."""

    __tablename__ = "certificate_renewal"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        CheckConstraint("trigger IN ('manual', 'auto')", name="trigger"),
        CheckConstraint("version >= 1", name="version"),
        Index("ix_certificate_renewal_lookup", "tenant_id", "certificate", "started_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    certificate: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    change_set_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    not_after: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    acme_provider: Mapped[str | None] = mapped_column(Text)
    challenge: Mapped[str | None] = mapped_column(Text)


__all__ = ["AcmeProviderRow", "CertificateRenewalRow", "DnsProviderRow"]


class DnsRecordRow(Base):
    """``dns_record``: an address record the controller created for a virtual service (ADR 0193).

    The row is the proof of ownership: the controller changes or deletes only records it has a
    row for. ``handle`` is the provider's opaque id for the record.
    """

    __tablename__ = "dns_record"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        UniqueConstraint("tenant_id", "hostname", "rtype"),
        CheckConstraint("rtype IN ('A', 'AAAA')", name="rtype"),
        CheckConstraint("state IN ('pending', 'published', 'error')", name="state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    version: Mapped[int] = mapped_column(nullable=False, server_default=text("1"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    virtual_service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    hostname: Mapped[str] = mapped_column(Text, nullable=False)
    rtype: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    ttl: Mapped[int] = mapped_column(nullable=False)
    proxied: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    handle: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
