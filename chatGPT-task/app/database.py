"""SQLite + SQLAlchemy 2.0 database layer.

The watcher and worker run in background threads, so the SQLite connection is
created with ``check_same_thread=False``.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./chatgpt_task.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def get_db() -> Session:
    """Open a new database session.

    The caller is responsible for closing the session.
    """
    return SessionLocal()
