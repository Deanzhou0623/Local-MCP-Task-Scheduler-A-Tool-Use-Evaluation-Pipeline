"""FastAPI application assembly: lifespan, error handlers, and routers.

Two exception handlers translate failures into the spec envelope:

- :class:`AppError` -> its declared code + mapped HTTP status.
- FastAPI ``RequestValidationError`` (bad/extra fields) -> ``VALIDATION_ERROR``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import router, scheduler_router, trace_router
from app.core.database import Base, engine
from app.core.errors import VALIDATION_ERROR, AppError, error_envelope
from app.jobs import models  # noqa: F401 - ensure tables are registered


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Local MCP Task Scheduler", version="1.0.0", lifespan=lifespan)


@app.exception_handler(AppError)
async def _app_error_handler(_request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_envelope())


@app.exception_handler(RequestValidationError)
async def _validation_handler(_request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0]
    loc = [str(p) for p in first.get("loc", ()) if p not in ("body", "query")]
    field = ".".join(loc) or None
    return JSONResponse(
        status_code=422,
        content=error_envelope(
            VALIDATION_ERROR, first.get("msg", "Invalid input."), field=field
        ),
    )


app.include_router(router)
app.include_router(trace_router)
app.include_router(scheduler_router)
