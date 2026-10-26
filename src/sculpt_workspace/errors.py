"""Domain errors shared by the HTTP API and MCP tools."""

from __future__ import annotations

from typing import Any


class SculptError(Exception):
    status: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFound(SculptError):
    status = 404
    code = "not_found"


class Conflict(SculptError):
    status = 409
    code = "conflict"


class StaleRevision(Conflict):
    code = "stale_revision"


class InvalidRequest(SculptError):
    status = 422
    code = "invalid_request"


class PayloadTooLarge(SculptError):
    status = 413
    code = "upload_too_large"


class QueueFull(SculptError):
    status = 503
    code = "queue_full"


class BlenderUnavailable(SculptError):
    status = 503
    code = "blender_unavailable"


class PublicationError(SculptError):
    status = 500
    code = "publication_failed"
