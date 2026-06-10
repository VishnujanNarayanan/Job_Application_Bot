"""Alembic runtime — connects to Neon using DATABASE_URL from .env.

Reads .env via python-dotenv so contributors don't have to export the
env var manually. Imports the SQLAlchemy declarative ``Base`` so Alembic's
autogenerate (Iteration 2+) can diff models against the live schema.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from src.state.models import Base

# Load .env BEFORE reading env vars.
load_dotenv()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL is not set. Add it to .env (see .env.example)."
    )
# Strip all whitespace — editors occasionally save a stale buffer that
# reintroduces spaces into the URL (e.g. "require ").
database_url = "".join(database_url.split())

# .env stores the standard `postgresql://` URL (works with psql and Neon's
# console). SQLAlchemy defaults that scheme to psycopg2 — we use psycopg3,
# so rewrite the prefix here. Keeps .env portable across tools.
if database_url.startswith("postgresql://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]

config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (rarely used; kept for completeness)."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to Neon and apply pending migrations."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
