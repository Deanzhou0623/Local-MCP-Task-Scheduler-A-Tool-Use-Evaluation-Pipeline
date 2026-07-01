"""SQLite + SQLAlchemy 2.0 database layer.

The watcher and worker run in background threads, so the SQLite connection is
created with ``check_same_thread=False``.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
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


def init_database(db_engine: Engine = engine) -> None:
    """Create tables and apply small local SQLite compatibility migrations.

    The project intentionally does not use Alembic yet. ``create_all`` is enough
    for fresh local databases, but it will not alter an existing SQLite file when
    specs add columns. Keep this narrow to local/dev schema drift.
    """
    Base.metadata.create_all(bind=db_engine)
    _ensure_sqlite_job_runs_spec06_columns(db_engine)


def _ensure_sqlite_job_runs_spec06_columns(db_engine: Engine) -> None:
    if db_engine.dialect.name != "sqlite":
        return
    if not inspect(db_engine).has_table("job_runs"):
        return

    column_sql = {
        "scheduled_bucket_hour": "ALTER TABLE job_runs ADD COLUMN scheduled_bucket_hour VARCHAR(10)",
        "scheduled_bucket_shard": "ALTER TABLE job_runs ADD COLUMN scheduled_bucket_shard INTEGER",
        "attempt_group_id": "ALTER TABLE job_runs ADD COLUMN attempt_group_id VARCHAR(48)",
        "attempt_number": "ALTER TABLE job_runs ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1",
        "parent_run_id": "ALTER TABLE job_runs ADD COLUMN parent_run_id VARCHAR(40)",
        "trigger_reason": "ALTER TABLE job_runs ADD COLUMN trigger_reason VARCHAR(16) NOT NULL DEFAULT 'scheduled'",
        "priority": "ALTER TABLE job_runs ADD COLUMN priority INTEGER NOT NULL DEFAULT 10",
        "deadline_at": "ALTER TABLE job_runs ADD COLUMN deadline_at DATETIME",
    }

    with db_engine.begin() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(job_runs)")
        }
        for column, statement in column_sql.items():
            if column not in existing:
                conn.exec_driver_sql(statement)

        conn.exec_driver_sql(
            """
            UPDATE job_runs
               SET scheduled_bucket_hour = strftime('%Y%m%d%H', scheduled_at)
             WHERE scheduled_bucket_hour IS NULL
            """
        )
        conn.exec_driver_sql(
            """
            UPDATE job_runs
               SET scheduled_bucket_shard = 0
             WHERE scheduled_bucket_shard IS NULL
            """
        )
        conn.exec_driver_sql(
            """
            UPDATE job_runs
               SET scheduled_bucket = scheduled_bucket_hour || '#S000'
             WHERE scheduled_bucket NOT LIKE '%#S%'
            """
        )
        conn.exec_driver_sql(
            """
            CREATE INDEX IF NOT EXISTS idx_runs_bucket_hour_status_sched
            ON job_runs (scheduled_bucket_hour, status, scheduled_at)
            """
        )
        conn.exec_driver_sql(
            """
            CREATE INDEX IF NOT EXISTS idx_runs_attempt_group
            ON job_runs (attempt_group_id, attempt_number)
            """
        )
