from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from atguigu_ai.auth import AuthBase


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = AuthBase.metadata


def _connection_url() -> URL:
    supplied_url = config.attributes.get("connection_url")
    if isinstance(supplied_url, URL):
        return supplied_url

    from ecs_demo.actions.db import build_database_url

    return build_database_url()


def _validate_target(database_url: URL) -> None:
    database = database_url.database
    host = database_url.host
    port = database_url.port or 3306
    actual_target = f"{host}:{port}/{database}"
    expected_target = os.environ.get("MIGRATION_EXPECTED_TARGET")

    if not expected_target:
        raise RuntimeError("MIGRATION_EXPECTED_TARGET is required")
    if actual_target != expected_target:
        raise RuntimeError("Migration target does not match the expected target")


def run_migrations_offline() -> None:
    database_url = _connection_url()
    _validate_target(database_url)
    credential_free_url = URL.create(
        drivername=database_url.drivername,
        host=database_url.host,
        port=database_url.port,
        database=database_url.database,
        query=database_url.query,
    )

    context.configure(
        url=credential_free_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    database_url = _connection_url()
    _validate_target(database_url)
    connectable = create_engine(database_url, poolclass=NullPool)

    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
