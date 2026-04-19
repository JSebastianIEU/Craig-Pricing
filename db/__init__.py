"""
Database package — SQLAlchemy setup + session helpers.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

from .models import Base

# Default: SQLite file in project root.
# On mounted filesystems (like cloud-synced folders) SQLite can fail with I/O errors.
# Set CRAIG_DB_PATH to a local path if you hit that — e.g. ~/craig.db
DB_PATH = os.environ.get(
    "CRAIG_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "craig.db"),
)

DATABASE_URL = os.environ.get(
    "CRAIG_DATABASE_URL",
    f"sqlite:///{DB_PATH}",
)


def _build_engine(url: str):
    """
    Build the SQLAlchemy engine.

    Two supported modes:
      - SQLite (default for local dev + tests) — single file, needs
        `check_same_thread=False` for FastAPI.
      - Postgres via Cloud SQL (production) — set CRAIG_DATABASE_URL to a
        `postgresql+pg8000://...` URL. On Cloud Run we connect over a Unix
        socket at /cloudsql/PROJECT:REGION:INSTANCE, which looks like:
            postgresql+pg8000://USER:PASS@/DBNAME?unix_sock=/cloudsql/...
        On Cloud Run the SQL proxy is wired in via `--add-cloudsql-instances`.
    """
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    # Postgres: a small pool that tolerates Cloud Run container restarts.
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=2,
        pool_recycle=1800,
    )


engine = _build_engine(DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Create all tables. Idempotent — safe to run on every boot."""
    Base.metadata.create_all(bind=engine)


def get_db() -> Session:
    """FastAPI dependency — yields a session and closes it after use."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session() -> Session:
    """Context manager for scripts that need a session outside FastAPI."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
