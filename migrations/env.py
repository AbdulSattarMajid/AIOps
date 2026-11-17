"""Alembic environment configuration.

Reads DATABASE_URL from the environment (after dotenv is applied) and
converts it to the psycopg dialect URL that SQLAlchemy 2.x requires.
No URL or password is printed or logged.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# Load .env so DATABASE_URL is available when running alembic from the CLI.
load_dotenv()

# Alembic Config object – gives access to the values in alembic.ini.
config = context.config

# Set up Python logging from the alembic.ini [loggers] section.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import the metadata so autogenerate can compare models to the DB.
from aiops.db.models import Base  # noqa: E402

target_metadata = Base.metadata


def _get_url() -> str:
    """Return the connection URL for the current environment.

    Uses DATABASE_URL from the environment, coercing it to the
    postgresql+psycopg dialect.  Never prints the value.
    """
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Export it before running alembic (e.g. export DATABASE_URL=...)."
        )
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg" + raw[len("postgresql"):]
    if raw.startswith("postgres://"):
        return "postgresql+psycopg" + raw[len("postgres"):]
    return raw


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect to the database)."""
    # Override sqlalchemy.url from the environment – alembic.ini has no URL.
    url = _get_url()
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
