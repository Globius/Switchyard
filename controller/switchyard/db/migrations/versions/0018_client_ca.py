# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Client CAs for mutual TLS.

- ``certificate.purpose`` (``server`` or ``client_ca``) and ``certificate.crl_secret_ref``.
- ``vs_port.client_ca_id`` (a client CA certificate of the same tenant) and ``vs_port.client_verify``.

Revision ID: 0018
Revises: 0017
"""

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "certificate",
        sa.Column("purpose", sa.Text(), server_default=sa.text("'server'"), nullable=False),
    )
    op.add_column("certificate", sa.Column("crl_secret_ref", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_certificate_purpose"), "certificate", "purpose IN ('server', 'client_ca')"
    )
    op.add_column("vs_port", sa.Column("client_ca_id", sa.UUID(), nullable=True))
    op.add_column(
        "vs_port",
        sa.Column("client_verify", sa.Text(), server_default=sa.text("'required'"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_vs_port_client_verify"), "vs_port", "client_verify IN ('required', 'optional')"
    )
    op.create_foreign_key(
        op.f("fk_vs_port_tenant_id_client_ca_id_certificate"),
        "vs_port",
        "certificate",
        ["tenant_id", "client_ca_id"],
        ["tenant_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_vs_port_tenant_id_client_ca_id_certificate"), "vs_port", type_="foreignkey"
    )
    op.drop_constraint(op.f("ck_vs_port_client_verify"), "vs_port", type_="check")
    op.drop_column("vs_port", "client_verify")
    op.drop_column("vs_port", "client_ca_id")
    op.drop_constraint(op.f("ck_certificate_purpose"), "certificate", type_="check")
    op.drop_column("certificate", "crl_secret_ref")
    op.drop_column("certificate", "purpose")
