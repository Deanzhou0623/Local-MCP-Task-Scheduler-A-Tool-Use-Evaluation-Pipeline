"""Shared fixtures: an isolated in-memory SQLite database per test.

The service layer is called directly with the ``db`` session. The tool layer
and FastAPI app open their *own* sessions via the module-level ``SessionLocal``,
so ``isolated_db`` repoints those names (in every module that imported them) at
a single in-memory engine. This keeps tool/API tests hermetic without touching
the on-disk database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base

# Modules that captured ``SessionLocal`` / ``engine`` at import time and must be
# repointed at the test engine.
_SESSION_LOCAL_MODULES = (
    "app.core.database",
    "app.api.deps",
    "app.mcp.registry",
    "app.scheduler.watcher",
    "app.scheduler.worker",
)
_ENGINE_MODULES = ("app.core.database", "app.api.server")


@pytest.fixture()
def isolated_db(monkeypatch):
    """Point every layer at one shared in-memory engine for the test."""
    import importlib

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)

    for name in _SESSION_LOCAL_MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "SessionLocal"):
            monkeypatch.setattr(module, "SessionLocal", TestSession)
    for name in _ENGINE_MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "engine"):
            monkeypatch.setattr(module, "engine", engine)

    yield TestSession
    engine.dispose()


@pytest.fixture()
def frozen_clock():
    """Pin ``now`` so relative-time requests are deterministic and gradeable.

    Yields a controller with ``.now`` (the pinned reference instant) and
    ``.set(dt)`` to repin within a test. The wall clock is restored afterwards.
    """
    from datetime import datetime

    from app.core import clock

    class _Clock:
        def __init__(self, ref: datetime) -> None:
            self.set(ref)

        def set(self, ref: datetime) -> datetime:
            clock.set_now(ref)
            self.now = clock.now_utc()
            return self.now

    # Default reference: 2026-06-09 12:00:00 UTC (= 05:00 in America/Vancouver).
    controller = _Clock(datetime(2026, 6, 9, 12, 0, 0))
    try:
        yield controller
    finally:
        clock.reset()


@pytest.fixture()
def db(isolated_db) -> Session:
    """A session for calling service functions directly."""
    session = isolated_db()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(isolated_db):
    """FastAPI TestClient wired to the isolated database."""
    from fastapi.testclient import TestClient

    from app.api import app, get_session

    def _override():
        session = isolated_db()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
