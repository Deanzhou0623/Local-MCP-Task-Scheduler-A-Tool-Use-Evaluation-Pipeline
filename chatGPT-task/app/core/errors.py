"""Structured error contract shared by the API and MCP tool layers.

Every failure surfaces through the same envelope (spec 01, section 10)::

    {"ok": false, "error": {"code", "message", "field?", "expected?"}}

Service code raises :class:`AppError`; the FastAPI layer and the MCP tool
registry both convert it into this envelope so a tool call and an HTTP call see
identical error shapes.
"""

from __future__ import annotations

# Required error codes (spec 01, section 10).
VALIDATION_ERROR = "VALIDATION_ERROR"
NOT_FOUND = "NOT_FOUND"
PERMISSION_DENIED = "PERMISSION_DENIED"
CONFLICT = "CONFLICT"
UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
INTERNAL_ERROR = "INTERNAL_ERROR"

# Map each error code to an HTTP status for the REST surface.
HTTP_STATUS = {
    VALIDATION_ERROR: 422,
    NOT_FOUND: 404,
    PERMISSION_DENIED: 403,
    CONFLICT: 409,
    UNSUPPORTED_ACTION: 422,
    INTERNAL_ERROR: 500,
}


def error_envelope(
    code: str,
    message: str,
    *,
    field: str | None = None,
    expected: str | None = None,
) -> dict:
    """Build the canonical error envelope, omitting empty optional keys."""
    error: dict = {"code": code, "message": message}
    if field is not None:
        error["field"] = field
    if expected is not None:
        error["expected"] = expected
    return {"ok": False, "error": error}


class AppError(Exception):
    """Domain error that carries a structured error contract.

    Raised by the service layer and translated into :func:`error_envelope`
    output by whichever surface (API or MCP tool) caught it.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        expected: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.expected = expected

    def to_envelope(self) -> dict:
        return error_envelope(
            self.code, self.message, field=self.field, expected=self.expected
        )

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)
