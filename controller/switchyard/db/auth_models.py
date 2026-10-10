"""SQLAlchemy models for user login: identity providers, sessions and the break-glass admin.

These tables are system-wide (not tenant-owned) and only the admin database role reads and writes
them during authentication, before a tenant is known. Secrets are never stored: providers keep a
``client_secret_ref`` and the break-glass admin a ``totp_secret_ref``, both into the secrets
backend.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from switchyard.db.base import Base, enum_check
from switchyard.domain.workflow import Role


def _timestamp(*, nullable: bool = True, default_now: bool = False) -> Mapped[datetime]:
    return mapped_column(
        TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=text("now()") if default_now else None,
    )


class IdentityProviderRow(Base):
    """``identity_provider``: an OIDC provider users can log in with."""

    __tablename__ = "identity_provider"
    __table_args__ = (
        UniqueConstraint("name"),
        CheckConstraint("protocol IN ('oidc', 'saml')", name="protocol"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    protocol: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'oidc'"))
    issuer_url: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret_ref: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'openid profile email'")
    )
    username_claim: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'preferred_username'")
    )
    email_claim: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'email'"))
    groups_claim: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'groups'"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class IdpRoleMappingRow(Base):
    """``idp_role_mapping``: a provider group grants a role in a tenant (or system-admin)."""

    __tablename__ = "idp_role_mapping"
    __table_args__ = (
        ForeignKeyConstraint(["provider_id"], ["identity_provider.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["scope_tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        UniqueConstraint("provider_id", "group_name", "scope_tenant_id"),
        enum_check("role", Role),
        CheckConstraint(
            "(role = 'system-admin') = (scope_tenant_id IS NULL)",
            name="system_role_has_no_tenant",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    provider_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    group_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: Not called ``tenant_id``: this table is system-wide, not tenant-owned (no RLS).
    scope_tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    role: Mapped[str] = mapped_column(Text, nullable=False)


class LoginStateRow(Base):
    """``login_state``: the state, nonce and PKCE verifier of a login in progress."""

    __tablename__ = "login_state"
    __table_args__ = (
        ForeignKeyConstraint(["provider_id"], ["identity_provider.id"], ondelete="CASCADE"),
    )

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    provider_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    nonce: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    return_to: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'/'"))
    expires_at: Mapped[datetime] = _timestamp(nullable=False)


class UserSessionRow(Base):
    """``user_session``: a browser session; only the SHA-256 of the cookie value is stored."""

    __tablename__ = "user_session"
    __table_args__ = (
        UniqueConstraint("token_hash"),
        ForeignKeyConstraint(["provider_id"], ["identity_provider.id"], ondelete="SET NULL"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    csrf_token: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    provider_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    grants: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    break_glass: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    expires_at: Mapped[datetime] = _timestamp(nullable=False)
    last_seen_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    revoked_at: Mapped[datetime | None] = _timestamp()


class LocalAdminRow(Base):
    """``local_admin``: the break-glass administrator (password plus TOTP), off unless enabled."""

    __tablename__ = "local_admin"

    username: Mapped[str] = mapped_column(Text, primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    totp_secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = _timestamp()
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
