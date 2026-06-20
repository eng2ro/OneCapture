"""Alembic environment.

The DB URL is resolved in this order:
1. ``sqlalchemy.url`` set on the Alembic config (programmatic runs / tests),
2. the ``ALEMBIC_DATABASE_URL`` env var,
3. ``eclaim.config.get_settings().database_url`` (the app's ``.env``).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from eclaim.config import get_settings
from eclaim.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    return (
        config.get_main_option("sqlalchemy.url")
        or os.environ.get("ALEMBIC_DATABASE_URL")
        or get_settings().database_url
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
