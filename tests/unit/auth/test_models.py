from collections import Counter
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import CheckConstraint, DateTime, JSON, String, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable


@pytest.fixture(scope="module")
def models():
    from atguigu_ai.auth import (
        Account,
        AccountRole,
        AccountStatus,
        AccountUserBinding,
        AuditEvent,
        AuditResult,
        AuthBase,
    )

    return SimpleNamespace(
        Account=Account,
        AccountRole=AccountRole,
        AccountStatus=AccountStatus,
        AccountUserBinding=AccountUserBinding,
        AuditEvent=AuditEvent,
        AuditResult=AuditResult,
        AuthBase=AuthBase,
    )


def _assert_string(column, *, length, nullable, primary_key=False):
    assert isinstance(column.type, String)
    assert column.type.length == length
    assert column.nullable is nullable
    assert column.primary_key is primary_key


def _assert_datetime(column, *, nullable):
    assert isinstance(column.type, DateTime)
    assert column.nullable is nullable
    assert getattr(column.type, "fsp", None) == 6


def _unique_column_sets(table):
    unique_sets = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    unique_sets.update(
        (column.name,) for column in table.columns if column.unique is True
    )
    return unique_sets


def _index_column_sets(table):
    return Counter(
        (tuple(index.columns.keys()), bool(index.unique)) for index in table.indexes
    )


def _check_expressions(table):
    return {
        _normalize_check_expression(str(constraint.sqltext))
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def _normalize_check_expression(expression):
    expression = " ".join(expression.lower().split())
    expression = re.sub(r"\(\s*", "(", expression)
    expression = re.sub(r"\s*\)", ")", expression)
    return re.sub(r"\s*,\s*", ",", expression)


def test_auth_exports_exact_string_enum_values(models):
    assert {member.value for member in models.AccountRole} == {"consumer", "admin"}
    assert {member.value for member in models.AccountStatus} == {
        "pending",
        "active",
        "disabled",
    }
    assert {member.value for member in models.AuditResult} == {"success", "failure"}


def test_auth_metadata_contains_only_the_three_account_tables(models):
    assert models.Account.__tablename__ == "account"
    assert models.AccountUserBinding.__tablename__ == "account_user_binding"
    assert models.AuditEvent.__tablename__ == "audit_event"
    assert set(models.AuthBase.metadata.tables) == {
        "account",
        "account_user_binding",
        "audit_event",
    }


def test_auth_metadata_is_self_contained_for_mysql_ddl(models):
    tables = models.AuthBase.metadata.sorted_tables

    assert {table.name for table in tables} == {
        "account",
        "account_user_binding",
        "audit_event",
    }
    for table in tables:
        assert str(CreateTable(table).compile(dialect=mysql.dialect()))


def test_account_columns_match_the_storage_contract(models):
    table = models.Account.__table__

    assert set(table.columns.keys()) == {
        "account_id",
        "email",
        "email_normalized",
        "password_hash",
        "role",
        "status",
        "email_verified_at",
        "created_at",
        "updated_at",
    }
    _assert_string(table.c.account_id, length=36, nullable=False, primary_key=True)
    _assert_string(table.c.email, length=254, nullable=False)
    _assert_string(table.c.email_normalized, length=254, nullable=False)
    _assert_string(table.c.password_hash, length=255, nullable=False)
    _assert_string(table.c.role, length=16, nullable=False)
    _assert_string(table.c.status, length=16, nullable=False)
    _assert_datetime(table.c.email_verified_at, nullable=True)
    _assert_datetime(table.c.created_at, nullable=False)
    _assert_datetime(table.c.updated_at, nullable=False)
    assert _unique_column_sets(table) == {("email_normalized",)}


def test_account_checks_and_indexes_match_the_contract(models):
    table = models.Account.__table__
    checks = _check_expressions(table)

    assert checks == {
        "role in ('consumer','admin')",
        "status in ('pending','active','disabled')",
    }
    assert _index_column_sets(table) == Counter(
        {
            (("status", "created_at"), False): 1,
            (("role", "status"), False): 1,
        }
    )


def test_account_user_binding_matches_the_one_to_one_contract(models):
    table = models.AccountUserBinding.__table__

    assert set(table.columns.keys()) == {
        "account_id",
        "user_id",
        "seed_version",
        "initialized_at",
    }
    _assert_string(table.c.account_id, length=36, nullable=False, primary_key=True)
    _assert_string(table.c.user_id, length=50, nullable=False)
    _assert_string(table.c.seed_version, length=32, nullable=False)
    _assert_datetime(table.c.initialized_at, nullable=False)
    assert _unique_column_sets(table) == {("user_id",)}

    foreign_keys = {
        foreign_key.parent.name: (
            foreign_key.target_fullname,
            foreign_key.ondelete,
        )
        for foreign_key in table.foreign_keys
    }
    assert foreign_keys == {
        "account_id": ("account.account_id", "CASCADE"),
    }
    assert not table.c.user_id.foreign_keys


def test_audit_event_columns_match_the_storage_contract(models):
    table = models.AuditEvent.__table__

    assert set(table.columns.keys()) == {
        "event_id",
        "request_id",
        "actor_account_id",
        "actor_role",
        "event_type",
        "target_type",
        "target_id",
        "result",
        "metadata_json",
        "created_at",
    }
    _assert_string(table.c.event_id, length=36, nullable=False, primary_key=True)
    _assert_string(table.c.request_id, length=64, nullable=False)
    _assert_string(table.c.actor_account_id, length=80, nullable=True)
    _assert_string(table.c.actor_role, length=16, nullable=False)
    _assert_string(table.c.event_type, length=64, nullable=False)
    _assert_string(table.c.target_type, length=32, nullable=True)
    _assert_string(table.c.target_id, length=64, nullable=True)
    _assert_string(table.c.result, length=16, nullable=False)
    assert isinstance(table.c.metadata_json.type, JSON)
    assert table.c.metadata_json.nullable is True
    _assert_datetime(table.c.created_at, nullable=False)


def test_audit_event_result_check_indexes_and_actor_reference_match_contract(models):
    table = models.AuditEvent.__table__
    checks = _check_expressions(table)

    assert checks == {"result in ('success','failure')"}
    assert _index_column_sets(table) == Counter(
        {
            (("request_id",), False): 1,
            (("actor_account_id", "created_at"), False): 1,
            (("event_type", "created_at"), False): 1,
            (("target_type", "target_id"), False): 1,
            (("created_at",), False): 1,
        }
    )
    assert not table.c.actor_account_id.foreign_keys
