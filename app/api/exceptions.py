"""Custom exceptions and error codes for Mobile API.

Provides standardized error handling with correlation IDs and detailed error messages.
"""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Standard error codes for API responses.

    These are the coarse-grained codes used by exception classes and match the
    OpenAPI spec (docs/openapi/mobile_api.yaml). For fine-grained wire codes used
    in direct response construction see app.api.models.responses.ErrorCode.
    """

    # Client errors (4xx)
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    SESSION_EXPIRED = "SESSION_EXPIRED"

    # Token/Auth-specific errors
    AUTH_EXPIRED = "TOKEN_EXPIRED"
    AUTH_INVALID = "TOKEN_INVALID"
    AUTH_REVOKED = "TOKEN_REVOKED"
    AUTH_WRONG_TYPE = "TOKEN_WRONG_TYPE"
    REFRESH_RATE_LIMITED = "REFRESH_RATE_LIMITED"
    GITHUB_OAUTH_STATE_INVALID = "oauth_state_invalid"
    GITHUB_TOKEN_EXCHANGE_FAILED = "github_token_exchange_failed"
    GITHUB_TOKEN_INVALID = "github_token_invalid"
    GITHUB_OAUTH_RATE_LIMITED = "github_oauth_rate_limited"

    # Sync-specific errors
    SYNC_SESSION_EXPIRED = "SYNC_SESSION_EXPIRED"
    SYNC_SESSION_NOT_FOUND = "SYNC_SESSION_NOT_FOUND"
    SYNC_SESSION_FORBIDDEN = "SYNC_SESSION_FORBIDDEN"
    SYNC_NO_CHANGES = "SYNC_NO_CHANGES"
    SYNC_CONFLICT = "SYNC_CONFLICT"
    SYNC_INVALID_ENTITY = "SYNC_INVALID_ENTITY"
    SYNC_ENTITY_NOT_FOUND = "SYNC_ENTITY_NOT_FOUND"

    # Server errors (5xx)
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    EXTERNAL_API_ERROR = "EXTERNAL_API_ERROR"
    PROCESSING_ERROR = "PROCESSING_ERROR"
    AUTH_SERVICE_UNAVAILABLE = "AUTH_SERVICE_UNAVAILABLE"

    # Configuration errors
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    FEATURE_DISABLED = "FEATURE_DISABLED"


class ErrorType(StrEnum):
    """Categories of errors for client handling.

    These match the OpenAPI spec. For the ErrorType used in response models
    see app.api.models.responses.ErrorType (which extends this set).
    """

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMIT = "rate_limit"
    EXTERNAL_SERVICE = "external_service"
    SYNC = "sync"
    INTERNAL = "internal"
    CONFIGURATION = "configuration"


# Mapping from ErrorCode to ErrorType
_ERROR_TYPE_MAP: dict[ErrorCode, ErrorType] = {
    ErrorCode.VALIDATION_ERROR: ErrorType.VALIDATION,
    ErrorCode.UNAUTHORIZED: ErrorType.AUTHENTICATION,
    ErrorCode.FORBIDDEN: ErrorType.AUTHORIZATION,
    ErrorCode.NOT_FOUND: ErrorType.NOT_FOUND,
    ErrorCode.CONFLICT: ErrorType.CONFLICT,
    ErrorCode.RATE_LIMIT_EXCEEDED: ErrorType.RATE_LIMIT,
    ErrorCode.SESSION_EXPIRED: ErrorType.AUTHENTICATION,
    ErrorCode.AUTH_EXPIRED: ErrorType.AUTHENTICATION,
    ErrorCode.AUTH_INVALID: ErrorType.AUTHENTICATION,
    ErrorCode.AUTH_REVOKED: ErrorType.AUTHENTICATION,
    ErrorCode.AUTH_WRONG_TYPE: ErrorType.AUTHENTICATION,
    ErrorCode.REFRESH_RATE_LIMITED: ErrorType.RATE_LIMIT,
    ErrorCode.GITHUB_OAUTH_STATE_INVALID: ErrorType.AUTHENTICATION,
    ErrorCode.GITHUB_TOKEN_EXCHANGE_FAILED: ErrorType.EXTERNAL_SERVICE,
    ErrorCode.GITHUB_TOKEN_INVALID: ErrorType.AUTHENTICATION,
    ErrorCode.GITHUB_OAUTH_RATE_LIMITED: ErrorType.RATE_LIMIT,
    ErrorCode.AUTH_SERVICE_UNAVAILABLE: ErrorType.INTERNAL,
    ErrorCode.SYNC_SESSION_EXPIRED: ErrorType.SYNC,
    ErrorCode.SYNC_SESSION_NOT_FOUND: ErrorType.SYNC,
    ErrorCode.SYNC_SESSION_FORBIDDEN: ErrorType.SYNC,
    ErrorCode.SYNC_NO_CHANGES: ErrorType.SYNC,
    ErrorCode.SYNC_CONFLICT: ErrorType.SYNC,
    ErrorCode.SYNC_INVALID_ENTITY: ErrorType.SYNC,
    ErrorCode.SYNC_ENTITY_NOT_FOUND: ErrorType.SYNC,
    ErrorCode.INTERNAL_ERROR: ErrorType.INTERNAL,
    ErrorCode.DATABASE_ERROR: ErrorType.INTERNAL,
    ErrorCode.EXTERNAL_API_ERROR: ErrorType.EXTERNAL_SERVICE,
    ErrorCode.PROCESSING_ERROR: ErrorType.INTERNAL,
    ErrorCode.CONFIGURATION_ERROR: ErrorType.CONFIGURATION,
    ErrorCode.FEATURE_DISABLED: ErrorType.CONFIGURATION,
}

# Retryable error codes
_RETRYABLE_CODES: set[ErrorCode] = {
    ErrorCode.RATE_LIMIT_EXCEEDED,
    ErrorCode.SESSION_EXPIRED,
    ErrorCode.AUTH_EXPIRED,  # Can retry with re-login
    ErrorCode.REFRESH_RATE_LIMITED,  # Can retry after delay
    ErrorCode.GITHUB_TOKEN_EXCHANGE_FAILED,
    ErrorCode.GITHUB_OAUTH_RATE_LIMITED,
    ErrorCode.AUTH_SERVICE_UNAVAILABLE,  # Temporary, can retry
    ErrorCode.SYNC_SESSION_EXPIRED,
    ErrorCode.DATABASE_ERROR,
    ErrorCode.EXTERNAL_API_ERROR,
}


class APIException(Exception):
    """Base exception for all API errors."""

    def __init__(
        self,
        message: str,
        error_code: ErrorCode,
        status_code: int = 500,
        details: dict[str, Any] | None = None,
        error_type: ErrorType | None = None,
        retryable: bool | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.details = details or {}
        self.error_type = error_type or _ERROR_TYPE_MAP.get(error_code, ErrorType.INTERNAL)
        self.retryable = retryable if retryable is not None else (error_code in _RETRYABLE_CODES)
        self.retry_after = retry_after


class ValidationError(APIException):
    """Raised when request validation fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(
            message=message,
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
            details=details,
        )


class AuthenticationError(APIException):
    """Raised when authentication fails."""

    def __init__(self, message: str = "Authentication failed"):
        super().__init__(
            message=message,
            error_code=ErrorCode.UNAUTHORIZED,
            status_code=401,
        )


class AuthorizationError(APIException):
    """Raised when user is not authorized to access a resource."""

    def __init__(self, message: str = "Access denied"):
        super().__init__(
            message=message,
            error_code=ErrorCode.FORBIDDEN,
            status_code=403,
        )


class ResourceNotFoundError(APIException):
    """Raised when a requested resource is not found."""

    def __init__(self, resource_type: str, resource_id: int | str):
        super().__init__(
            message=f"{resource_type} with ID {resource_id} not found",
            error_code=ErrorCode.NOT_FOUND,
            status_code=404,
            details={"resource_type": resource_type, "resource_id": str(resource_id)},
        )


class DuplicateResourceError(APIException):
    """Raised when attempting to create a duplicate resource."""

    def __init__(self, message: str, existing_id: int | str | None = None):
        details = {}
        if existing_id is not None:
            details["existing_id"] = str(existing_id)

        super().__init__(
            message=message,
            error_code=ErrorCode.CONFLICT,
            status_code=409,
            details=details,
        )


class RateLimitExceededError(APIException):
    """Raised when rate limit is exceeded."""

    def __init__(self, retry_after_seconds: int | None = None):
        message = "Rate limit exceeded"
        if retry_after_seconds:
            message += f". Try again in {retry_after_seconds} seconds"

        details = {}
        if retry_after_seconds:
            details["retry_after_seconds"] = retry_after_seconds

        super().__init__(
            message=message,
            error_code=ErrorCode.RATE_LIMIT_EXCEEDED,
            status_code=429,
            details=details,
            retry_after=retry_after_seconds,
        )


class DatabaseError(APIException):
    """Raised when database operations fail."""

    def __init__(self, message: str = "Database error occurred"):
        super().__init__(
            message=message,
            error_code=ErrorCode.DATABASE_ERROR,
            status_code=503,
        )


class ExternalAPIError(APIException):
    """Raised when external API calls fail."""

    def __init__(self, service_name: str, message: str | None = None):
        full_message = f"{service_name} API error"
        if message:
            full_message += f": {message}"

        super().__init__(
            message=full_message,
            error_code=ErrorCode.EXTERNAL_API_ERROR,
            status_code=502,
            details={"service": service_name},
        )


class ProcessingError(APIException):
    """Raised when request processing fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(
            message=message,
            error_code=ErrorCode.PROCESSING_ERROR,
            status_code=500,
            details=details,
        )


# Sync-specific exceptions


class SyncSessionExpiredError(APIException):
    """Raised when sync session has expired."""

    def __init__(self, session_id: str | None = None):
        details = {"session_id": session_id} if session_id else {}
        super().__init__(
            message="Sync session expired. Please start a new sync session.",
            error_code=ErrorCode.SYNC_SESSION_EXPIRED,
            status_code=410,
            details=details,
            retryable=True,
        )


class SyncSessionNotFoundError(APIException):
    """Raised when sync session is not found."""

    def __init__(self, session_id: str | None = None):
        details = {"session_id": session_id} if session_id else {}
        super().__init__(
            message="Sync session not found. Please start a new sync session.",
            error_code=ErrorCode.SYNC_SESSION_NOT_FOUND,
            status_code=404,
            details=details,
            retryable=True,
        )


class SyncSessionForbiddenError(APIException):
    """Raised when sync session belongs to another user/client."""

    def __init__(self) -> None:
        super().__init__(
            message="Sync session does not belong to this user or client.",
            error_code=ErrorCode.SYNC_SESSION_FORBIDDEN,
            status_code=403,
            retryable=False,
        )


class SyncNoChangesError(APIException):
    """Raised when delta sync has no changes since cursor."""

    def __init__(self, since: int):
        super().__init__(
            message="No changes since last sync.",
            error_code=ErrorCode.SYNC_NO_CHANGES,
            status_code=200,  # Not an error per se, but no data
            details={"since": since},
            retryable=False,
        )


class SyncConflictError(APIException):
    """Raised when sync apply encounters a version conflict."""

    def __init__(
        self,
        entity_type: str,
        entity_id: int | str,
        client_version: int,
        server_version: int,
    ):
        super().__init__(
            message=f"Version conflict for {entity_type} {entity_id}. "
            f"Client version {client_version}, server version {server_version}.",
            error_code=ErrorCode.SYNC_CONFLICT,
            status_code=409,
            details={
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "client_version": client_version,
                "server_version": server_version,
            },
            retryable=True,
        )


class SyncInvalidEntityError(APIException):
    """Raised when sync apply receives unsupported entity type."""

    def __init__(self, entity_type: str):
        super().__init__(
            message=f"Unsupported entity type for sync: {entity_type}",
            error_code=ErrorCode.SYNC_INVALID_ENTITY,
            status_code=400,
            details={"entity_type": entity_type},
            retryable=False,
        )


class SyncEntityNotFoundError(APIException):
    """Raised when sync apply cannot find entity to update."""

    def __init__(self, entity_type: str, entity_id: int | str):
        super().__init__(
            message=f"{entity_type} with ID {entity_id} not found for sync.",
            error_code=ErrorCode.SYNC_ENTITY_NOT_FOUND,
            status_code=404,
            details={"entity_type": entity_type, "entity_id": str(entity_id)},
            retryable=False,
        )


# Token/Refresh-specific exceptions


class TokenExpiredError(APIException):
    """Raised when JWT token has expired (401)."""

    def __init__(self, token_type: str = "access"):
        super().__init__(
            message=f"{token_type.capitalize()} token has expired. Please re-authenticate.",
            error_code=ErrorCode.AUTH_EXPIRED,
            status_code=401,
            details={"token_type": token_type},
            retryable=True,
        )


class TokenInvalidError(APIException):
    """Raised when JWT token is malformed or signature invalid (401)."""

    def __init__(self, reason: str | None = None):
        message = "Invalid token"
        if reason:
            message += f": {reason}"
        super().__init__(
            message=message,
            error_code=ErrorCode.AUTH_INVALID,
            status_code=401,
            details={"reason": reason} if reason else {},
            retryable=False,
        )


class TokenRevokedError(APIException):
    """Raised when refresh token has been revoked (401)."""

    def __init__(self) -> None:
        super().__init__(
            message="Token has been revoked. Please re-authenticate.",
            error_code=ErrorCode.AUTH_REVOKED,
            status_code=401,
            retryable=False,
        )


class TokenWrongTypeError(APIException):
    """Raised when wrong token type is used (e.g., access token for refresh) (401)."""

    def __init__(self, expected: str, received: str):
        super().__init__(
            message=f"Wrong token type. Expected {expected} token, got {received} token.",
            error_code=ErrorCode.AUTH_WRONG_TYPE,
            status_code=401,
            details={"expected": expected, "received": received},
            retryable=False,
        )


class RefreshRateLimitedError(APIException):
    """Raised when token refresh is rate limited (429)."""

    def __init__(self, retry_after_seconds: int | None = None):
        message = "Too many refresh attempts"
        if retry_after_seconds:
            message += f". Try again in {retry_after_seconds} seconds"

        super().__init__(
            message=message,
            error_code=ErrorCode.REFRESH_RATE_LIMITED,
            status_code=429,
            details={"retry_after_seconds": retry_after_seconds} if retry_after_seconds else {},
            retry_after=retry_after_seconds,
            retryable=True,
        )


class AuthServiceUnavailableError(APIException):
    """Raised when auth service is temporarily unavailable (503)."""

    def __init__(self, retry_after_seconds: int | None = None):
        message = "Authentication service temporarily unavailable"
        if retry_after_seconds:
            message += f". Try again in {retry_after_seconds} seconds"

        super().__init__(
            message=message,
            error_code=ErrorCode.AUTH_SERVICE_UNAVAILABLE,
            status_code=503,
            details={"retry_after_seconds": retry_after_seconds} if retry_after_seconds else {},
            retry_after=retry_after_seconds,
            retryable=True,
        )


# Configuration exceptions


class ConfigurationError(APIException):
    """Raised when server configuration is missing or invalid (500)."""

    def __init__(self, message: str, config_key: str | None = None):
        details = {"config_key": config_key} if config_key else {}
        super().__init__(
            message=message,
            error_code=ErrorCode.CONFIGURATION_ERROR,
            status_code=500,
            details=details,
            retryable=False,
        )


class FeatureDisabledError(APIException):
    """Raised when a feature is disabled by configuration (403)."""

    def __init__(self, feature: str, message: str | None = None):
        default_message = f"Feature '{feature}' is disabled"
        super().__init__(
            message=message or default_message,
            error_code=ErrorCode.FEATURE_DISABLED,
            status_code=403,
            details={"feature": feature},
            retryable=False,
        )
