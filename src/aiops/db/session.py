"""Database session helper.

Reads DATABASE_URL from the environment (after dotenv is loaded by the
calling process).  Returns None when DATABASE_URL is absent so that the
rest of the application can run without a database configured.

No connection is made at import time.
"""
from __future__ import annotations

import os
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def _coerce_url(raw: str) -> str:
    """Convert a plain postgresql:// URL to postgresql+psycopg:// for SQLAlchemy 2.x."""
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg" + raw[len("postgresql"):]
    if raw.startswith("postgres://"):
        # Heroku-style alias
        return "postgresql+psycopg" + raw[len("postgres"):]
    return raw


def get_engine() -> Optional[Engine]:
    """Return a SQLAlchemy Engine if DATABASE_URL is set, else None.

    The URL is never printed or logged.
    """
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        return None
    url = _coerce_url(raw)
    return create_engine(url, pool_pre_ping=True, future=True)


def get_session_factory(engine: Optional[Engine]) -> Optional[sessionmaker]:
    """Return a session factory bound to *engine*, or None."""
    if engine is None:
        return None
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
