"""Global exception handlers for the Mobile API.

Provides consistent error responses across all endpoints with correlation ID tracking.
"""

from http import HTTPStatus

from fastapi.exception_handlers import http_exception_handler as default_http_exception_handler
from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.exceptions import APIException, ErrorCode, ErrorType
from app.api.models.responses import error_response, make_error
from app.core.logging_utils import get_logger, redact_for_logging

logger = get_logger(__name__)

_HTTP_ERROR_METADATA: dict[int, tuple[ErrorCode, ErrorType, bool]] = {
    status.HTTP_400_BAD_REQUEST: (ErrorCode.VALIDATION_ERROR, ErrorType.VALIDATION, False),
    status.HTTP_401_UNAUTHORIZED: (ErrorCode.UNAUTHORIZED, ErrorType.AUTHENTICATION, False),
    status.HTTP_403_FORBIDDEN: (ErrorCode.FORBIDDEN, ErrorType.AUTHORIZATION, False),
    status.HTTP_404_NOT_FOUND: (ErrorCode.NOT_FOUND, ErrorType.NOT_FOUND, False),
    status.HTTP_409_CONFLICT: (ErrorCode.CONFLICT, ErrorType.CONFLICT, False),
    status.HTTP_422_UNPROCESSABLE_CONTENT: (
        ErrorCode.VALIDATION_ERROR,
        ErrorType.VALIDATION,
        False,
    ),
    status.HTTP_429_TOO_MANY_REQUESTS: (
        ErrorCode.RATE_LIMIT_EXCEEDED,
        ErrorType.RATE_LIMIT,
        True,
    ),
}


def _http_error_metadata(status_code: int) -> tuple[ErrorCode, ErrorType, bool]:
    mapped = _HTTP_ERROR_METADATA.get(status_code)
    if mapped is not None:
        return mapped
    if 400 <= status_code < 500:
        return ErrorCode.VALIDATION_ERROR, ErrorType.VALIDATION, False
    return ErrorCode.INTERNAL_ERROR, ErrorType.INTERNAL, False


def _safe_http_error_message(exc: StarletteHTTPException) -> str:
    if exc.status_code >= 500:
        return "An internal server error occurred"
    if isinstance(exc.detail, str) and exc.detail.strip():
        return exc.detail
    try:
        return HTTPStatus(exc.status_code).phrase
    except ValueError:
        return "Request failed"


def _is_business_api_path(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


async def api_exception_handler(request: Request, exc: Exception) -> Response:
    """Handle custom API exceptions."""
    # Type narrowing for FastAPI compatibility
    if not isinstance(exc, APIException):
        raise exc

    correlation_id = getattr(request.state, "correlation_id", None)

    # Log the error
    logger.error(
        f"API error: {exc.error_code.value} - {exc.message}",
        exc_info=False,
        extra={
            "correlation_id": correlation_id,
            "error_code": exc.error_code.value,
            "error_type": exc.error_type.value,
            "status_code": exc.status_code,
            "retryable": exc.retryable,
            "path": request.url.path,
        },
    )

    detail = make_error(
        code=exc.error_code.value,
        message=exc.message,
        error_type=exc.error_type.value,
        retryable=exc.retryable,
        details=exc.details or None,
        retry_after=exc.retry_after,
    )
    detail.correlation_id = correlation_id

    return JSONResponse(
        status_code=exc.status_code, content=error_response(detail, correlation_id=correlation_id)
    )


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """Normalize business API HTTP errors while preserving non-API semantics."""
    if not isinstance(exc, StarletteHTTPException):
        raise exc

    correlation_id = getattr(request.state, "correlation_id", None)
    if not _is_business_api_path(request.url.path):
        response = await default_http_exception_handler(request, exc)
        if correlation_id:
            response.headers["X-Correlation-ID"] = correlation_id
        return response

    error_code, error_type, retryable = _http_error_metadata(exc.status_code)
    detail = make_error(
        code=error_code.value,
        message=_safe_http_error_message(exc),
        error_type=error_type,
        retryable=retryable,
    )
    detail.correlation_id = correlation_id

    headers = dict(exc.headers or {})
    if correlation_id:
        headers["X-Correlation-ID"] = correlation_id
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(detail, correlation_id=correlation_id),
        headers=headers,
    )


def _format_validation_errors(errors: list[dict]) -> list[dict]:
    """Return validation errors without raw rejected input values."""
    formatted_errors = []
    for error in errors:
        field = ".".join(str(loc) for loc in error["loc"])
        formatted_errors.append(
            {
                "field": field,
                "message": redact_for_logging(error["msg"], key=field),
                "type": error["type"],
            }
        )
    return formatted_errors


async def validation_exception_handler(request: Request, exc: Exception) -> Response:
    """Handle FastAPI request validation errors without echoing secrets."""
    # Type narrowing for FastAPI compatibility
    if not isinstance(exc, RequestValidationError):
        raise exc

    correlation_id = getattr(request.state, "correlation_id", None)
    formatted_errors = _format_validation_errors(
        [
            {key: value for key, value in error.items() if key in {"loc", "msg", "type"}}
            for error in exc.errors()
        ]
    )

    logger.warning(
        "Request validation failed",
        extra={
            "correlation_id": correlation_id,
            "errors": formatted_errors,
            "path": request.url.path,
        },
    )

    detail = make_error(
        code=ErrorCode.VALIDATION_ERROR.value,
        message="Request validation failed",
        error_type=ErrorType.VALIDATION,
        retryable=False,
        details={"fields": formatted_errors},
    )
    detail.correlation_id = correlation_id

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=error_response(detail, correlation_id=correlation_id),
    )


async def pydantic_validation_exception_handler(request: Request, exc: Exception) -> Response:
    """Handle internal Pydantic model validation failures as server errors."""
    # Type narrowing for FastAPI compatibility
    if not isinstance(exc, PydanticValidationError):
        raise exc

    correlation_id = getattr(request.state, "correlation_id", None)

    # Response-model failures are implementation errors, not invalid client
    # requests. Keep both the response and log entry free of rejected values.
    logger.error(
        "Internal model validation failed",
        extra={"correlation_id": correlation_id, "path": request.url.path},
    )

    detail = make_error(
        code=ErrorCode.INTERNAL_ERROR.value,
        message="An internal server error occurred",
        error_type=ErrorType.INTERNAL,
        retryable=False,
    )
    detail.correlation_id = correlation_id

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_response(detail, correlation_id=correlation_id),
    )


async def database_exception_handler(request: Request, exc: Exception) -> Response:
    """Handle database-related exceptions."""
    correlation_id = getattr(request.state, "correlation_id", None)

    logger.error(
        f"Database error: {exc}",
        exc_info=True,
        extra={"correlation_id": correlation_id, "path": request.url.path},
    )

    detail = make_error(
        code=ErrorCode.DATABASE_ERROR.value,
        message="Database temporarily unavailable",
        error_type=ErrorType.INTERNAL,
        retryable=True,
    )
    detail.correlation_id = correlation_id

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=error_response(detail, correlation_id=correlation_id),
    )


async def global_exception_handler(request: Request, exc: Exception) -> Response:
    """Catch-all handler for unexpected exceptions."""
    correlation_id = getattr(request.state, "correlation_id", None)

    logger.error(
        "Unhandled exception",
        extra={
            "correlation_id": correlation_id,
            "exception_type": type(exc).__name__,
            "path": request.url.path,
        },
    )

    detail = make_error(
        code=ErrorCode.INTERNAL_ERROR.value,
        message="An internal server error occurred",
        error_type=ErrorType.INTERNAL,
        retryable=False,
    )
    detail.correlation_id = correlation_id

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_response(detail, correlation_id=correlation_id),
    )
