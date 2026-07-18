import re
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects.mysql import DATETIME, JSON, VARCHAR
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError, OperationalError

from ecs_demo.actions.db import build_database_url


pytestmark = pytest.mark.integration

_TEST_DATABASE_PATTERN = re.compile(r"^llm_cs_test_[0-9a-f]{32}$")
_DENIED_DATABASES = {
    "ecs",
    "mysql",
    "information_schema",
    "performance_schema",
    "sys",
}
_MANAGED_TABLES = {"account", "account_user_binding", "audit_event"}
_TEST_PASSWORD = "test-only-password"


def _validated_database_name(database_name: str) -> str:
    if (
        not _TEST_DATABASE_PATTERN.fullmatch(database_name)
        or database_name.lower() in _DENIED_DATABASES
    ):
        raise ValueError("Unsafe temporary database name")
    return database_name


def _quoted_database_name(database_name: str) -> str:
    return f"`{_validated_database_name(database_name)}`"


def _admin_engine(database_url: URL):
    return create_engine(
        database_url.set(database=None),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )


@contextmanager
def _isolated_mysql_database():
    database_name = f"llm_cs_test_{uuid4().hex}"
    _validated_database_name(database_name)
    base_url = build_database_url()
    database_url = base_url.set(database=database_name)
    admin_engine = _admin_engine(base_url)
    database_engine = None
    database_created = False
    active_error = None

    try:
        try:
            with admin_engine.connect() as connection:
                connection.execute(
                    text(
                        f"CREATE DATABASE {_quoted_database_name(database_name)} "
                        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                )
                database_created = True
        except OperationalError:
            raise AssertionError("Local MySQL is unavailable for integration tests") from None

        database_engine = create_engine(database_url, pool_pre_ping=True)
        with database_engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE user_info ("
                    "user_id VARCHAR(50) NOT NULL, "
                    "PRIMARY KEY (user_id)"
                    ") ENGINE=InnoDB"
                )
            )

        yield database_url
    except BaseException as error:
        active_error = error
        raise
    finally:
        cleanup_failed = False
        if database_engine is not None:
            try:
                database_engine.dispose()
            except BaseException:
                cleanup_failed = True
        try:
            admin_engine.dispose()
        except BaseException:
            cleanup_failed = True

        if database_created:
            cleanup_engine = None
            try:
                cleanup_engine = _admin_engine(base_url)
                with cleanup_engine.connect() as connection:
                    connection.execute(
                        text(
                            f"DROP DATABASE IF EXISTS "
                            f"{_quoted_database_name(database_name)}"
                        )
                    )
            except BaseException:
                cleanup_failed = True
            finally:
                if cleanup_engine is not None:
                    try:
                        cleanup_engine.dispose()
                    except BaseException:
                        cleanup_failed = True

        if cleanup_failed:
            cleanup_message = f"Failed to clean up temporary database {database_name}"
            if active_error is not None:
                active_error.add_note(cleanup_message)
            else:
                raise AssertionError(cleanup_message) from None


def _alembic_config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection_url"] = database_url
    return config


def _target_name(database_url: URL) -> str:
    port = database_url.port or 3306
    return f"{database_url.host}:{port}/{database_url.database}"


def _assert_credential_free(error: BaseException, database_url: URL) -> None:
    message = str(error)
    assert _TEST_PASSWORD not in message
    assert database_url.render_as_string(hide_password=False) not in message


def _managed_tables(database_url: URL) -> set[str]:
    engine = create_engine(database_url)
    try:
        return _MANAGED_TABLES.intersection(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _normalized_check_sql(sqltext: str) -> str:
    normalized = sqltext.lower().replace("`", "")
    normalized = re.sub(r"_[a-z0-9]+(?=')", "", normalized)
    normalized = re.sub(r"\s+", "", normalized)
    while normalized.startswith("(") and normalized.endswith(")"):
        depth = 0
        wraps_expression = True
        for index, character in enumerate(normalized):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(normalized) - 1:
                    wraps_expression = False
                    break
        if not wraps_expression:
            break
        normalized = normalized[1:-1]
    return normalized


def _table_contract(database_url: URL) -> None:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == {
            "account",
            "account_user_binding",
            "alembic_version",
            "audit_event",
            "user_info",
        }

        expected_columns = {
            "account": {
                "account_id": (VARCHAR, 36, False, None),
                "email": (VARCHAR, 254, False, None),
                "email_normalized": (VARCHAR, 254, False, None),
                "password_hash": (VARCHAR, 255, False, None),
                "role": (VARCHAR, 16, False, None),
                "status": (VARCHAR, 16, False, None),
                "email_verified_at": (DATETIME, 6, True, None),
                "created_at": (DATETIME, 6, False, "current_timestamp(6)"),
                "updated_at": (DATETIME, 6, False, "current_timestamp(6)"),
            },
            "account_user_binding": {
                "account_id": (VARCHAR, 36, False, None),
                "user_id": (VARCHAR, 50, False, None),
                "seed_version": (VARCHAR, 32, False, None),
                "initialized_at": (DATETIME, 6, False, "current_timestamp(6)"),
            },
            "audit_event": {
                "event_id": (VARCHAR, 36, False, None),
                "request_id": (VARCHAR, 64, False, None),
                "actor_account_id": (VARCHAR, 80, True, None),
                "actor_role": (VARCHAR, 16, False, None),
                "event_type": (VARCHAR, 64, False, None),
                "target_type": (VARCHAR, 32, True, None),
                "target_id": (VARCHAR, 64, True, None),
                "result": (VARCHAR, 16, False, None),
                "metadata_json": (JSON, None, True, None),
                "created_at": (DATETIME, 6, False, "current_timestamp(6)"),
            },
        }
        for table_name, expected in expected_columns.items():
            columns = {column["name"]: column for column in inspector.get_columns(table_name)}
            assert set(columns) == set(expected)
            for column_name, (type_class, size, nullable, default) in expected.items():
                column = columns[column_name]
                assert isinstance(column["type"], type_class)
                if type_class is VARCHAR:
                    assert column["type"].length == size
                elif type_class is DATETIME:
                    assert column["type"].fsp == size
                assert column["nullable"] is nullable
                actual_default = column["default"]
                if default is None:
                    assert actual_default is None
                else:
                    assert str(actual_default).lower().replace(" ", "") == default

        primary_keys = {
            table_name: inspector.get_pk_constraint(table_name)
            for table_name in expected_columns
        }
        assert primary_keys["account"]["constrained_columns"] == ["account_id"]
        assert primary_keys["account_user_binding"]["constrained_columns"] == [
            "account_id"
        ]
        assert primary_keys["audit_event"]["constrained_columns"] == ["event_id"]

        unique_contracts = {
            table_name: {
                constraint["name"]: tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(table_name)
            }
            for table_name in expected_columns
        }
        assert unique_contracts == {
            "account": {
                "uq_account_email_normalized": ("email_normalized",),
            },
            "account_user_binding": {
                "uq_account_user_binding_user_id": ("user_id",),
            },
            "audit_event": {},
        }

        foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("account_user_binding")
        }
        assert set(foreign_keys) == {
            "fk_account_user_binding_account_id_account",
            "fk_account_user_binding_user_id_user_info",
        }
        account_fk = foreign_keys["fk_account_user_binding_account_id_account"]
        assert account_fk["constrained_columns"] == ["account_id"]
        assert account_fk["referred_table"] == "account"
        assert account_fk["referred_columns"] == ["account_id"]
        assert account_fk["options"].get("ondelete", "").upper() == "CASCADE"
        user_fk = foreign_keys["fk_account_user_binding_user_id_user_info"]
        assert user_fk["constrained_columns"] == ["user_id"]
        assert user_fk["referred_table"] == "user_info"
        assert user_fk["referred_columns"] == ["user_id"]
        assert user_fk["options"].get("ondelete", "").upper() == "CASCADE"
        assert inspector.get_foreign_keys("account") == []
        assert inspector.get_foreign_keys("audit_event") == []

        check_contracts = {
            table_name: {
                constraint["name"]: _normalized_check_sql(constraint["sqltext"])
                for constraint in inspector.get_check_constraints(table_name)
            }
            for table_name in expected_columns
        }
        assert check_contracts == {
            "account": {
                "ck_account_role": "rolein('consumer','admin')",
                "ck_account_status": "statusin('pending','active','disabled')",
            },
            "account_user_binding": {},
            "audit_event": {
                "ck_audit_event_result": "resultin('success','failure')",
            },
        }

        indexes = {
            table_name: {
                index["name"]: tuple(index["column_names"])
                for index in inspector.get_indexes(table_name)
                if not index.get("unique", False)
            }
            for table_name in ("account", "audit_event")
        }
        assert indexes["account"] == {
            "ix_account_role_status": ("role", "status"),
            "ix_account_status_created_at": ("status", "created_at"),
        }
        assert indexes["audit_event"] == {
            "ix_audit_event_actor_account_id_created_at": (
                "actor_account_id",
                "created_at",
            ),
            "ix_audit_event_created_at": ("created_at",),
            "ix_audit_event_event_type_created_at": ("event_type", "created_at"),
            "ix_audit_event_request_id": ("request_id",),
            "ix_audit_event_target_type_target_id": ("target_type", "target_id"),
        }
    finally:
        engine.dispose()


def _insert_contract(database_url: URL) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO user_info (user_id) VALUES ('user-1'), ('user-2')")
            )
            connection.execute(
                text(
                    "INSERT INTO account "
                    "(account_id, email, email_normalized, password_hash, role, status) "
                    "VALUES ('account-1', 'User@Example.com', 'user@example.com', "
                    "'hash', 'consumer', 'active')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO account_user_binding "
                    "(account_id, user_id, seed_version) "
                    "VALUES ('account-1', 'user-1', 'v1')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO audit_event "
                    "(event_id, request_id, actor_account_id, actor_role, event_type, result) "
                    "VALUES ('event-1', 'request-1', 'account-1', 'consumer', "
                    "'account.login', 'success')"
                )
            )

        duplicate_email = (
            "INSERT INTO account "
            "(account_id, email, email_normalized, password_hash, role, status) "
            "VALUES ('account-2', 'user@example.com', 'user@example.com', "
            "'hash', 'consumer', 'active')"
        )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(duplicate_email))

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO account "
                    "(account_id, email, email_normalized, password_hash, role, status) "
                    "VALUES ('account-2', 'second@example.com', 'second@example.com', "
                    "'hash', 'consumer', 'active')"
                )
            )
        duplicate_binding = (
            "INSERT INTO account_user_binding "
            "(account_id, user_id, seed_version) "
            "VALUES ('account-2', 'user-1', 'v1')"
        )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(duplicate_binding))

        invalid_rows = (
            "INSERT INTO account "
            "(account_id, email, email_normalized, password_hash, role, status) "
            "VALUES ('bad-role', 'role@example.com', 'role@example.com', "
            "'hash', 'owner', 'active')",
            "INSERT INTO account "
            "(account_id, email, email_normalized, password_hash, role, status) "
            "VALUES ('bad-status', 'status@example.com', 'status@example.com', "
            "'hash', 'consumer', 'deleted')",
            "INSERT INTO audit_event "
            "(event_id, request_id, actor_role, event_type, result) "
            "VALUES ('bad-result', 'request-2', 'consumer', 'account.login', 'unknown')",
        )
        for statement in invalid_rows:
            with pytest.raises(OperationalError) as exc_info, engine.begin() as connection:
                connection.execute(text(statement))
            assert exc_info.value.orig.args[0] == 3819

        orphan_bindings = (
            "INSERT INTO account_user_binding "
            "(account_id, user_id, seed_version) "
            "VALUES ('missing-account', 'user-2', 'v1')",
            "INSERT INTO account_user_binding "
            "(account_id, user_id, seed_version) "
            "VALUES ('account-2', 'missing-user', 'v1')",
        )
        for statement in orphan_bindings:
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(text(statement))

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO account_user_binding "
                    "(account_id, user_id, seed_version) "
                    "VALUES ('account-2', 'user-2', 'v1')"
                )
            )
            connection.execute(text("DELETE FROM account WHERE account_id = 'account-1'"))
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM account_user_binding "
                    "WHERE account_id = 'account-1'"
                )
            ) == 0
            assert connection.scalar(
                text("SELECT COUNT(*) FROM audit_event WHERE event_id = 'event-1'")
            ) == 1

            connection.execute(text("DELETE FROM user_info WHERE user_id = 'user-2'"))
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM account_user_binding "
                    "WHERE account_id = 'account-2'"
                )
            ) == 0
            assert connection.scalar(
                text("SELECT COUNT(*) FROM account WHERE account_id = 'account-2'")
            ) == 1
    finally:
        engine.dispose()


def _assert_only_baseline_table_remains(database_url: URL) -> None:
    engine = create_engine(database_url)
    try:
        assert set(inspect(engine).get_table_names()) == {
            "alembic_version",
            "user_info",
        }
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM alembic_version")) == 0
    finally:
        engine.dispose()


def test_account_schema_upgrade_downgrade_and_repeatability(monkeypatch) -> None:
    with _isolated_mysql_database() as database_url:
        monkeypatch.setenv("MIGRATION_EXPECTED_TARGET", _target_name(database_url))
        config = _alembic_config(database_url)

        command.upgrade(config, "head")
        _table_contract(database_url)
        _insert_contract(database_url)

        command.downgrade(config, "base")
        _assert_only_baseline_table_remains(database_url)

        command.upgrade(config, "head")
        _table_contract(database_url)


@pytest.mark.parametrize("gate_value", [None, "wrong.example:3306/not_the_target"])
def test_migration_target_gate_rejects_before_managed_tables_are_created(
    monkeypatch,
    gate_value,
) -> None:
    with _isolated_mysql_database() as database_url:
        guarded_url = database_url.set(password=_TEST_PASSWORD)
        config = _alembic_config(guarded_url)
        if gate_value is None:
            monkeypatch.delenv("MIGRATION_EXPECTED_TARGET", raising=False)
        else:
            monkeypatch.setenv("MIGRATION_EXPECTED_TARGET", gate_value)

        with pytest.raises(RuntimeError) as exc_info:
            command.upgrade(config, "head")

        _assert_credential_free(exc_info.value, guarded_url)
        assert _managed_tables(database_url) == set()


def test_supplied_url_does_not_import_database_environment_module() -> None:
    child_code = r'''
import os
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL

for name in ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD"):
    os.environ.pop(name, None)
os.environ["MIGRATION_EXPECTED_TARGET"] = "offline.test:3306/offline_schema"

class RejectDatabaseModuleImport:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "ecs_demo.actions.db":
            raise RuntimeError("database environment module imported")
        return None

sys.meta_path.insert(0, RejectDatabaseModuleImport())
config = Config("alembic.ini")
config.attributes["connection_url"] = URL.create(
    "mysql+pymysql",
    username="offline-user",
    password="test-only-password",
    host="offline.test",
    port=3306,
    database="offline_schema",
)
command.upgrade(config, "head", sql=True)
print("SUPPLIED_URL_OK")
'''
    environment = os.environ.copy()
    result = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    combined_output = result.stdout + result.stderr

    assert result.returncode == 0, combined_output
    assert "SUPPLIED_URL_OK" in result.stdout
    assert _TEST_PASSWORD not in combined_output
    assert "mysql+pymysql://" not in combined_output


def test_preexisting_managed_table_blocks_upgrade_without_partial_schema(
    monkeypatch,
) -> None:
    with _isolated_mysql_database() as database_url:
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE account (sentinel VARCHAR(16) NOT NULL) "
                        "ENGINE=InnoDB"
                    )
                )
        finally:
            engine.dispose()

        monkeypatch.setenv("MIGRATION_EXPECTED_TARGET", _target_name(database_url))
        config = _alembic_config(database_url)
        with pytest.raises(RuntimeError) as exc_info:
            command.upgrade(config, "head")

        _assert_credential_free(exc_info.value, database_url)
        assert _managed_tables(database_url) == {"account"}
        verification_engine = create_engine(database_url)
        try:
            columns = inspect(verification_engine).get_columns("account")
            assert [column["name"] for column in columns] == ["sentinel"]
        finally:
            verification_engine.dispose()


def test_offline_upgrade_emits_schema_without_credentials(monkeypatch, capsys) -> None:
    database_url = URL.create(
        "mysql+pymysql",
        username="offline-user",
        password=_TEST_PASSWORD,
        host="offline.test",
        port=3306,
        database="offline_schema",
    )
    monkeypatch.setenv("MIGRATION_EXPECTED_TARGET", _target_name(database_url))
    config = _alembic_config(database_url)

    command.upgrade(config, "head", sql=True)

    output = capsys.readouterr().out.lower()
    assert "create table account " in output
    assert "create table account_user_binding " in output
    assert "create table audit_event " in output
    assert _TEST_PASSWORD not in output
    assert database_url.render_as_string(hide_password=False).lower() not in output
