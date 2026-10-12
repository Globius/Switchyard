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
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
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
        CheckConstraint(
            "(protocol = 'oidc' AND issuer_url IS NOT NULL AND client_id IS NOT NULL) "
            "OR (protocol = 'saml' AND saml IS NOT NULL)",
            name="settings_match_protocol",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    protocol: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'oidc'"))
    issuer_url: Mapped[str | None] = mapped_column(Text)
    client_id: Mapped[str | None] = mapped_column(Text)
    #: SAML settings (``idp_entity_id``, ``sso_url``, ``signing_certificates``, ...); no secrets.
    saml: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
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
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class IdpTestRow(Base):
    """``idp_test``: a test sign-in; its outcome is kept five minutes and read once."""

    __tablename__ = "idp_test"
    __table_args__ = (
        ForeignKeyConstraint(["provider_id"], ["identity_provider.id"], ondelete="CASCADE"),
        CheckConstraint("status IN ('pending', 'done')", name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    provider_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    expires_at: Mapped[datetime] = _timestamp(nullable=False)


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
    #: Free text for opaque group ids (Entra ID object ids), so screens can show a name.
    label: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'system:cli'")
    )
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class UserAccountRow(Base):
    """``user_account``: everyone seen at sign-in. No secrets, passwords, tokens or other claims."""

    __tablename__ = "user_account"
    __table_args__ = (
        ForeignKeyConstraint(["provider_id"], ["identity_provider.id"], ondelete="CASCADE"),
        UniqueConstraint("provider_id", "subject", name="uq_user_account_provider_subject"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    provider_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    #: Groups as last returned by the provider (at most 200).
    groups: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    first_login_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    last_login_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    last_seen_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    disabled_at: Mapped[datetime | None] = _timestamp()


class LoginStateRow(Base):
    """``login_state``: the state, nonce and PKCE verifier of a login in progress."""

    __tablename__ = "login_state"
    __table_args__ = (
        ForeignKeyConstraint(["provider_id"], ["identity_provider.id"], ondelete="CASCADE"),
    )

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    provider_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    nonce: Mapped[str | None] = mapped_column(Text)
    code_verifier: Mapped[str | None] = mapped_column(Text)
    #: SAML: the ``AuthnRequest`` id the response must answer (``InResponseTo``).
    request_id: Mapped[str | None] = mapped_column(Text)
    return_to: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'/'"))
    #: ``login`` (creates a session) or ``test`` (an administrator's test sign-in: no session).
    purpose: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'login'"))
    test_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    expires_at: Mapped[datetime] = _timestamp(nullable=False)


class SamlAssertionSeenRow(Base):
    """``saml_assertion_seen``: assertion ids already accepted, kept until they expire (replay)."""

    __tablename__ = "saml_assertion_seen"
    __table_args__ = (
        ForeignKeyConstraint(["provider_id"], ["identity_provider.id"], ondelete="CASCADE"),
    )

    provider_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    assertion_id: Mapped[str] = mapped_column(Text, primary_key=True)
    expires_at: Mapped[datetime] = _timestamp(nullable=False)


class UserSessionRow(Base):
    """``user_session``: a browser session; only the SHA-256 of the cookie value is stored."""

    __tablename__ = "user_session"
    __table_args__ = (
        UniqueConstraint("token_hash"),
        ForeignKeyConstraint(["provider_id"], ["identity_provider.id"], ondelete="SET NULL"),
        ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        Index("ix_user_session_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    csrf_token: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    provider_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    #: The person (null for break-glass, which has no ``user_account`` row).
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    #: Groups the provider returned at sign-in; grants are recomputed from these (ADR 0030).
    groups: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    grants_computed_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
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
    #: Null until the TOTP enrollment was confirmed with a valid code; such an admin cannot log in.
    totp_confirmed_at: Mapped[datetime | None] = _timestamp()
    last_login_at: Mapped[datetime | None] = _timestamp()
    #: Last accepted TOTP time step (unix time // 30); a code for this step or earlier is refused.
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)


class LoginThrottleRow(Base):
    """``login_throttle``: failed sign-ins per key and until when the key is blocked."""

    __tablename__ = "login_throttle"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    blocked_until: Mapped[datetime | None] = _timestamp()
    updated_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class SystemSettingRow(Base):
    """``system_setting``: a controller-wide switch set by a system administrator."""

    __tablename__ = "system_setting"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    updated_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class ControllerSettingsRow(Base):
    """``controller_settings``: the confirmed public URL and the change waiting to be confirmed."""

    __tablename__ = "controller_settings"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default=text("1"))
    public_url: Mapped[str | None] = mapped_column(Text)
    pending: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    updated_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class BreakGlassSettingRow(Base):
    """``break_glass_setting``: the single row that says whether break-glass was switched on."""

    __tablename__ = "break_glass_setting"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default=text("1"))
    enabled_until: Mapped[datetime | None] = _timestamp()
    enabled_by: Mapped[str | None] = mapped_column(Text)
    enabled_at: Mapped[datetime | None] = _timestamp()
    reason: Mapped[str | None] = mapped_column(Text)
