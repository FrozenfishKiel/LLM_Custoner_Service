"""Add the account schema baseline.

Revision ID: 20260718_0001
Revises:
Create Date: 2026-07-18

"""
from collections.abc import Sequence

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql


revision: str = "20260718_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MANAGED_TABLES = {"account", "account_user_binding", "audit_event"}
_CURRENT_TIMESTAMP = sa.text("CURRENT_TIMESTAMP(6)")


def upgrade() -> None:
    if not context.is_offline_mode():
        existing_tables = set(inspect(op.get_bind()).get_table_names())
        if existing_tables & _MANAGED_TABLES:
            raise RuntimeError("Account migration target tables already exist")

    op.create_table(
        "account",
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("email_normalized", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("email_verified_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=_CURRENT_TIMESTAMP,
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=_CURRENT_TIMESTAMP,
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('consumer','admin')",
            name="ck_account_role",
        ),
        sa.CheckConstraint(
            "status IN ('pending','active','disabled')",
            name="ck_account_status",
        ),
        sa.PrimaryKeyConstraint("account_id", name="pk_account"),
        sa.UniqueConstraint(
            "email_normalized",
            name="uq_account_email_normalized",
        ),
    )
    op.create_index(
        "ix_account_role_status",
        "account",
        ["role", "status"],
        unique=False,
    )
    op.create_index(
        "ix_account_status_created_at",
        "account",
        ["status", "created_at"],
        unique=False,
    )

    op.create_table(
        "account_user_binding",
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=50), nullable=False),
        sa.Column("seed_version", sa.String(length=32), nullable=False),
        sa.Column(
            "initialized_at",
            mysql.DATETIME(fsp=6),
            server_default=_CURRENT_TIMESTAMP,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.account_id"],
            name="fk_account_user_binding_account_id_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_info.user_id"],
            name="fk_account_user_binding_user_id_user_info",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", name="pk_account_user_binding"),
        sa.UniqueConstraint(
            "user_id",
            name="uq_account_user_binding_user_id",
        ),
    )

    op.create_table(
        "audit_event",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("actor_account_id", sa.String(length=80), nullable=True),
        sa.Column("actor_role", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=_CURRENT_TIMESTAMP,
            nullable=False,
        ),
        sa.CheckConstraint(
            "result IN ('success','failure')",
            name="ck_audit_event_result",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_audit_event"),
    )
    op.create_index(
        "ix_audit_event_actor_account_id_created_at",
        "audit_event",
        ["actor_account_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_event_created_at",
        "audit_event",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_event_event_type_created_at",
        "audit_event",
        ["event_type", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_event_request_id",
        "audit_event",
        ["request_id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_event_target_type_target_id",
        "audit_event",
        ["target_type", "target_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("audit_event")
    op.drop_table("account_user_binding")
    op.drop_table("account")
