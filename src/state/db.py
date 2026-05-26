"""Layer 7 — SQLAlchemy engine + session factory.

Single place for every layer to acquire a database session. Reads
DATABASE_URL from .env at import time, rewrites the URL to use psycopg3
(SQLAlchemy's default for ``postgresql://`` is psycopg2, which we don't
install), and exposes a ``session_scope()`` context manager that handles
commit/rollback automatically.

Iteration 1: synchronous engine. Async swap planned for Iteration 2.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import yaml
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

load_dotenv()


def _resolve_database_url() -> str:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError(
            "DATABASE_URL is not set. Add it to .env (see .env.example)."
        )
    # Same prefix rewrite as src/state/migrations/env.py — keeps .env portable.
    if raw.startswith("postgresql://"):
        raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
    return raw


def _load_pool_config() -> dict:
    """Read database pool settings from config/config.yaml."""
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "config",
        "config.yaml",
    )
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("database", {})


_db_cfg = _load_pool_config()

engine = create_engine(
    _resolve_database_url(),
    pool_size=_db_cfg.get("pool_size", 2),
    max_overflow=_db_cfg.get("max_overflow", 5),
    pool_pre_ping=_db_cfg.get("pool_pre_ping", True),
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a session; commit on success, rollback on exception, always close."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
