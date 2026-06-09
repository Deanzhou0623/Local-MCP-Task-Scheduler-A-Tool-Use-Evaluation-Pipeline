"""Shared FastAPI dependencies."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.database import SessionLocal


def get_session() -> Session:
    """Yield a request-scoped database session and always close it.

    Tests override this via ``app.dependency_overrides`` to inject an isolated
    in-memory session.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
