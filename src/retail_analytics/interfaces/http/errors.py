"""One error envelope for every failure: ``{"error": {code, message, details}}``.

Rules: a record that is not the caller's answers exactly like a missing one
(404 ``not_found``); authentication failures are uniform (401
``unauthenticated``, never the reason); messages are fixed or come from
application errors whose messages are safe to show. Unexpected failures
become 503 ``unavailable`` without internals: every write is idempotent by
``submission_key``, so the client can retry the same request.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from retail_analytics.application.authentication import AuthenticationFailed
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.persistence import (
    ActiveRunExists,
    IdempotencyConflict,
    PersistenceError,
    RecordNotFound,
)
from retail_analytics.application.investigations import RunNotActive
from retail_analytics.domain.persona import PersonaError, PersonaErrorCode
from retail_analytics.domain.report_deletion import DeletionError, DeletionErrorCode
from retail_analytics.domain.reports import ReportError, ReportErrorCode

_log = logging.getLogger("retail_analytics.http")


class ApiError(Exception):
    """An error the interface raises itself (bad header, service missing)."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers
        super().__init__(f"{code}: {message}")


UNAUTHENTICATED = ApiError(
    401,
    "unauthenticated",
    "A valid bearer token is required.",
    headers={"WWW-Authenticate": "Bearer"},
)
NOT_FOUND_MESSAGE = "Not found."


def error_response(
    status: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "details": details or {}}},
        status_code=status,
        headers=headers,
    )


def to_api_error(error: Exception) -> ApiError:
    """Translate an application error; unknown errors become ``unavailable``."""
    match error:
        case ApiError():
            return error
        case AuthenticationFailed():
            return UNAUTHENTICATED
        case AccessDenied(kind="permission"):
            return ApiError(
                403, "forbidden", "Your account may not perform this operation."
            )
        case AccessDenied() | RecordNotFound(kind="session" | "run"):
            return ApiError(404, "not_found", NOT_FOUND_MESSAGE)
        case RecordNotFound(kind="run event"):
            return ApiError(
                400,
                "invalid_cursor",
                "Last-Event-ID does not belong to this run.",
            )
        case RecordNotFound():
            return ApiError(404, "not_found", NOT_FOUND_MESSAGE)
        case ActiveRunExists():
            return ApiError(
                409,
                "active_run_exists",
                "The session already has an active investigation. Send a message "
                "to steer it or queue a separate request.",
                details={"active_run_id": error.active_run_id},
            )
        case RunNotActive():
            return ApiError(
                409,
                "run_not_active",
                "The investigation is not accepting this input any more.",
            )
        case IdempotencyConflict():
            return ApiError(
                409,
                "idempotency_conflict",
                "This submission_key was already used for a different request.",
            )
        case ReportError():
            return ApiError(_REPORT_STATUS[error.code], error.code.value, error.message)
        case DeletionError():
            return ApiError(
                _DELETION_STATUS[error.code], error.code.value, error.message
            )
        case PersonaError():
            return ApiError(
                _PERSONA_STATUS[error.code],
                error.code.value,
                error.message,
                details={
                    "findings": [
                        {"kind": f.kind.value, "severity": f.severity.value}
                        for f in error.findings
                    ]
                },
            )
        case ValueError():
            # Raised by use-case input checks (empty or overlong text).
            return ApiError(422, "invalid_request", str(error))
    _log.error("unexpected %s in HTTP request", type(error).__name__)
    return ApiError(
        503,
        "unavailable",
        "The service could not complete the request. Retry it with the same "
        "submission_key.",
    )


_REPORT_STATUS: dict[ReportErrorCode, int] = {
    ReportErrorCode.INVALID_REQUEST: 422,
    ReportErrorCode.INVALID_DRAFT: 422,
    ReportErrorCode.UNDECLARED_CITATION: 422,
    ReportErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ReportErrorCode.STALE_BASE_VERSION: 409,
    ReportErrorCode.EVIDENCE_UNAVAILABLE: 409,
    ReportErrorCode.ACCESS_CHANGED: 409,
}

_PERSONA_STATUS: dict[PersonaErrorCode, int] = {
    PersonaErrorCode.INVALID_REQUEST: 422,
    PersonaErrorCode.NOT_FOUND: 404,
    PersonaErrorCode.SENSITIVE_CONTENT: 422,
    PersonaErrorCode.POLICY_CONFLICT: 422,
    PersonaErrorCode.CONFLICT: 409,
    PersonaErrorCode.NOT_PREVIEWED: 409,
    PersonaErrorCode.IDEMPOTENCY_CONFLICT: 409,
    PersonaErrorCode.NOT_A_DRAFT: 409,
    PersonaErrorCode.NOT_PUBLISHED_BEFORE: 409,
}

_DELETION_STATUS: dict[DeletionErrorCode, int] = {
    DeletionErrorCode.INVALID_REQUEST: 422,
    DeletionErrorCode.TOO_MANY_PENDING: 409,
    DeletionErrorCode.IDEMPOTENCY_CONFLICT: 409,
    DeletionErrorCode.EXPIRED: 410,
    DeletionErrorCode.ALREADY_RESOLVED: 409,
    DeletionErrorCode.STALE: 409,
}


def install_error_handlers(app: FastAPI) -> None:
    async def handle(_: Request, error: Exception) -> JSONResponse:
        api = to_api_error(error)
        return error_response(
            api.status, api.code, api.message, api.details, api.headers
        )

    async def handle_validation(_: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, RequestValidationError)  # noqa: S101
        problems = [
            {
                "location": [str(part) for part in item.get("loc", ())],
                "problem": item.get("msg", "invalid"),
            }
            for item in error.errors()
        ]
        return error_response(
            422,
            "invalid_request",
            "The request is malformed.",
            {"problems": problems},
        )

    async def handle_http(_: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, StarletteHTTPException)  # noqa: S101
        code = {404: "not_found", 405: "method_not_allowed"}.get(
            error.status_code, "http_error"
        )
        message = NOT_FOUND_MESSAGE if error.status_code == 404 else str(error.detail)
        return error_response(error.status_code, code, message)

    app.add_exception_handler(RequestValidationError, handle_validation)
    app.add_exception_handler(StarletteHTTPException, handle_http)
    for kind in (
        ApiError,
        AuthenticationFailed,
        AccessDenied,
        PersistenceError,
        RunNotActive,
        ReportError,
        DeletionError,
        PersonaError,
        ValueError,
        # Anything else (reached through Starlette's server-error middleware).
        Exception,
    ):
        app.add_exception_handler(kind, handle)
