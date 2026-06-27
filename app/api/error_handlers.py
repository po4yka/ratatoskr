"""Global exception handlers for the Mobile API.

Provides consistent error responses across all endpoints with correlation ID tracking.
"""

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError as PydanticValidationError

from app.api.exceptions import APIException, ErrorCode, ErrorType
from app.api.models.responses import error_response, make_error
from app.config import load_config
from app.core.logging_utils import get_logger, redact_for_logging

logger = get_logger(__name__)


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
    """Handle Pydantic and FastAPI request validation errors without echoing secrets."""
    # Type narrowing for FastAPI compatibility
    if not isinstance(exc, (PydanticValidationError, RequestValidationError)):
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
        f"Unhandled exception: {exc}",
        exc_info=True,
        extra={"correlation_id": correlation_id, "path": request.url.path},
    )

    # Don't leak error details in production. This MUST key off the deployment
    # environment, not the log level: an operator raising LOG_LEVEL=DEBUG for
    # troubleshooting must never start returning raw exception text (DB hosts,
    # internal URLs) to API clients (CWE-209). Fail safe to production behavior
    # if the deployment flag is unavailable.
    config = load_config()
    is_production = getattr(getattr(config, "deployment", None), "is_production_mode", True)

    message = str(exc) if not is_production else "An internal server error occurred"

    detail = make_error(
        code=ErrorCode.INTERNAL_ERROR.value,
        message=message,
        error_type=ErrorType.INTERNAL,
        retryable=False,
    )
    detail.correlation_id = correlation_id

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_response(detail, correlation_id=correlation_id),
    )
