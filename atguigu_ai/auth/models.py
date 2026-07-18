from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class AccountRole(str, Enum):
    consumer = "consumer"
    admin = "admin"


class AccountStatus(str, Enum):
    pending = "pending"
    active = "active"
    disabled = "disabled"


class AuditResult(str, Enum):
    success = "success"
    failure = "failure"


class AuthBase(DeclarativeBase):
    pass


_current_timestamp = text("CURRENT_TIMESTAMP(6)")


class Account(AuthBase):
    __tablename__ = "account"
    __table_args__ = (
        PrimaryKeyConstraint("account_id", name="pk_account"),
        UniqueConstraint("email_normalized", name="uq_account_email_normalized"),
        CheckConstraint(
            "role IN ('consumer','admin')",
            name="ck_account_role",
        ),
        CheckConstraint(
            "status IN ('pending','active','disabled')",
            name="ck_account_status",
        ),
        Index("ix_account_status_created_at", "status", "created_at"),
        Index("ix_account_role_status", "role", "status"),
    )

    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(254), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        nullable=False,
        server_default=_current_timestamp,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        nullable=False,
        server_default=_current_timestamp,
    )


class AccountUserBinding(AuthBase):
    __tablename__ = "account_user_binding"
    __table_args__ = (
        PrimaryKeyConstraint("account_id", name="pk_account_user_binding"),
        UniqueConstraint("user_id", name="uq_account_user_binding_user_id"),
    )

    account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "account.account_id",
            name="fk_account_user_binding_account_id_account",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    seed_version: Mapped[str] = mapped_column(String(32), nullable=False)
    initialized_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        nullable=False,
        server_default=_current_timestamp,
    )


class AuditEvent(AuthBase):
    __tablename__ = "audit_event"
    __table_args__ = (
        PrimaryKeyConstraint("event_id", name="pk_audit_event"),
        CheckConstraint(
            "result IN ('success','failure')",
            name="ck_audit_event_result",
        ),
        Index("ix_audit_event_request_id", "request_id"),
        Index(
            "ix_audit_event_actor_account_id_created_at",
            "actor_account_id",
            "created_at",
        ),
        Index(
            "ix_audit_event_event_type_created_at",
            "event_type",
            "created_at",
        ),
        Index(
            "ix_audit_event_target_type_target_id",
            "target_type",
            "target_id",
        ),
        Index("ix_audit_event_created_at", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_account_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    actor_role: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        nullable=False,
        server_default=_current_timestamp,
    )
