"""REST routes for the job resource (spec 01, section 5).

These five endpoints back the five MCP tools; both call the same service
functions, so behavior and the error contract stay identical. New resources get
their own router module and are mounted in :mod:`app.api.server`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.jobs.schemas import (
    CreateJobRequest,
    DeleteJobRequest,
    ListJobsParams,
    ModifyJobRequest,
)
from app.jobs.service import (
    create_job,
    delete_job,
    get_job,
    get_trace,
    list_jobs,
    modify_job,
)

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

# Execution traces live under their own resource (spec 04, section 8).
trace_router = APIRouter(prefix="/v1/traces", tags=["traces"])


@trace_router.get("/{trace_id}")
def trace_detail(
    trace_id: str,
    user_id: str = Query(...),
    db: Session = Depends(get_session),
) -> dict:
    return get_trace(db, trace_id=trace_id, user_id=user_id)


@router.post("")
def create(req: CreateJobRequest, db: Session = Depends(get_session)) -> dict:
    return create_job(db, req)


@router.get("")
def list_(
    user_id: str = Query(...),
    status: Optional[str] = Query(None),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
    page_size: int = Query(20, ge=1, le=100),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_session),
) -> dict:
    params = ListJobsParams(
        user_id=user_id,
        status=status,
        start_time=start_time,
        end_time=end_time,
        page_size=page_size,
        page=page,
    )
    return list_jobs(db, params)


@router.get("/{job_id}")
def detail(
    job_id: str,
    user_id: str = Query(...),
    db: Session = Depends(get_session),
) -> dict:
    return get_job(db, job_id=job_id, user_id=user_id)


@router.patch("/{job_id}")
def modify(
    job_id: str,
    req: ModifyJobRequest,
    db: Session = Depends(get_session),
) -> dict:
    return modify_job(db, job_id=job_id, req=req)


@router.delete("/{job_id}")
def delete(
    job_id: str,
    req: DeleteJobRequest,
    db: Session = Depends(get_session),
) -> dict:
    return delete_job(db, job_id=job_id, user_id=req.user_id)
