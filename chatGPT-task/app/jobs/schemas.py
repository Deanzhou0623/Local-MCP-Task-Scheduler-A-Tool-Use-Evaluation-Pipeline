"""Pydantic request models (spec 01 validation rules).

Every model sets ``extra="forbid"`` because the spec rejects unknown fields on
create and modify — an LLM that hallucinates an argument should fail loudly
rather than have it silently dropped.

Two flavors of each mutating request exist:

- API models (``CreateJobRequest``, ``ModifyJobRequest``, ``DeleteJobRequest``)
  take ``job_id`` from the URL path, so it is not part of the body.
- ``*ToolArgs`` models carry ``job_id`` inside the argument object, matching the
  MCP tool-call shape in the spec.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

JobType = Literal["immediate", "one_time", "recurring"]
EditableStatus = Literal["scheduled", "paused"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Create ---------------------------------------------------------------
class JobParamsCreate(_Strict):
    type: JobType
    time: Optional[datetime] = None
    schedule: Optional[str] = None
    timezone: Optional[str] = None


class CreateJobRequest(_Strict):
    user_id: str
    action: str
    job_params: JobParamsCreate


# --- List -----------------------------------------------------------------
class ListJobsParams(_Strict):
    user_id: str
    status: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    page_size: int = Field(default=20, ge=1, le=100)
    page: int = Field(default=1, ge=1)


# --- Get ------------------------------------------------------------------
class GetJobToolArgs(_Strict):
    user_id: str
    job_id: str


# --- Modify ---------------------------------------------------------------
class JobParamsModify(_Strict):
    type: Optional[JobType] = None
    time: Optional[datetime] = None
    schedule: Optional[str] = None
    timezone: Optional[str] = None


class ModifyJobRequest(_Strict):
    """PATCH body — ``job_id`` comes from the path."""

    user_id: str
    action: Optional[str] = None
    status: Optional[EditableStatus] = None
    job_params: Optional[JobParamsModify] = None


class ModifyJobToolArgs(ModifyJobRequest):
    """MCP tool args — ``job_id`` is part of the argument object."""

    job_id: str


# --- Delete ---------------------------------------------------------------
class DeleteJobRequest(_Strict):
    """DELETE body — ``job_id`` comes from the path."""

    user_id: str


class DeleteJobToolArgs(_Strict):
    user_id: str
    job_id: str
