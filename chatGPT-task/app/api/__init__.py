"""REST surface. ``app`` is re-exported so ``uvicorn app.api:app`` works."""

from app.api.deps import get_session
from app.api.server import app

__all__ = ["app", "get_session"]
