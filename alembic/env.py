"""Alembic environment for the crypto-ai-anal Postgres schema.

The database URL is resolved, in priority order, from:

  1. ``DATABASE_URL``      (set by the ``db-init`` compose service)
  2. ``PGHOST``/``PGPORT``/``POSTGRES_USER``/``POSTGRES_PASSWORD``/``POSTGRES_DB``
     (the PG* environment variables used by the psql path; handy locally)

Migrations are pure SQL (no ORM models are used for the schema), so
``target_metadata`` is intentionally left ``None``. ``compare_type`` is on so
future autogenerate diffs surface column-type drift.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "marketflow")
    password = os.getenv("PGPASSWORD", os.getenv("POSTGRES_PASSWORD", ""))
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    db = os.getenv("POSTGRES_DB", "marketflow")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


DATABASE_URL = _database_url()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live database connection."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection (sync, psycopg2 driver)."""
    engine = create_engine(DATABASE_URL, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=None,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
