"""Common API response models and helpers."""

from __future__ import annotations

import os
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.api.context import correlation_id_ctx
from app.api.exceptions import ErrorType
from app.application.dto.stream_enums import ProcessingStage, ProgressEventKind  # noqa: F401
from app.core.time_utils import UTC


class ErrorCode(StrEnum):
    """Structured error codes for programmatic handling."""

    AUTH_EXPIRED = "AUTH_TOKEN_EXPIRED"
    AUTH_INVALID = "AUTH_TOKEN_INVALID"
    AUTH_SESSION_EXPIRED = "AUTH_SESSION_EXPIRED"
    AUTH_LOGIN_INVALID = "AUTH_CREDENTIALS_INVALID"
    AUTH_ACCOUNT_LOCKED = "AUTH_SECRET_LOCKED"
    AUTH_ACCESS_REVOKED = "AUTH_SECRET_REVOKED"
    AUTHZ_USER_NOT_ALLOWED = "AUTHZ_USER_NOT_ALLOWED"
    AUTHZ_CLIENT_NOT_ALLOWED = "AUTHZ_CLIENT_NOT_ALLOWED"
    AUTHZ_OWNER_REQUIRED = "AUTHZ_OWNER_REQUIRED"
    AUTHZ_ACCESS_DENIED = "AUTHZ_ACCESS_DENIED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    VALIDATION_FIELD_REQUIRED = "VALIDATION_FIELD_REQUIRED"
    VALIDATION_FIELD_INVALID = "VALIDATION_FIELD_INVALID"
    VALIDATION_URL_INVALID = "VALIDATION_URL_INVALID"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    RESOURCE_ALREADY_EXISTS = "RESOURCE_ALREADY_EXISTS"
    RESOURCE_VERSION_CONFLICT = "RESOURCE_VERSION_CONFLICT"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    EXTERNAL_FIRECRAWL_ERROR = "EXTERNAL_FIRECRAWL_ERROR"
    EXTERNAL_OPENROUTER_ERROR = "EXTERNAL_OPENROUTER_ERROR"
    EXTERNAL_TELEGRAM_ERROR = "EXTERNAL_TELEGRAM_ERROR"
    EXTERNAL_SERVICE_TIMEOUT = "EXTERNAL_SERVICE_TIMEOUT"
    EXTERNAL_SERVICE_UNAVAILABLE = "EXTERNAL_SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INTERNAL_DATABASE_ERROR = "INTERNAL_DATABASE_ERROR"
    INTERNAL_CONFIG_ERROR = "INTERNAL_CONFIG_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    AUTH_REVOKED = "TOKEN_REVOKED"
    AUTH_WRONG_TYPE = "TOKEN_WRONG_TYPE"
    REFRESH_RATE_LIMITED = "REFRESH_RATE_LIMITED"
    AUTH_SERVICE_UNAVAILABLE = "AUTH_SERVICE_UNAVAILABLE"
    PROCESSING_ERROR = "PROCESSING_ERROR"
    EXTERNAL_API_ERROR = "EXTERNAL_API_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    FEATURE_DISABLED = "FEATURE_DISABLED"
    SYNC_SESSION_EXPIRED = "SYNC_SESSION_EXPIRED"
    SYNC_SESSION_NOT_FOUND = "SYNC_SESSION_NOT_FOUND"
    SYNC_SESSION_FORBIDDEN = "SYNC_SESSION_FORBIDDEN"
    SYNC_NO_CHANGES = "SYNC_NO_CHANGES"
    SYNC_CONFLICT = "SYNC_CONFLICT"
    SYNC_INVALID_ENTITY = "SYNC_INVALID_ENTITY"
    SYNC_ENTITY_NOT_FOUND = "SYNC_ENTITY_NOT_FOUND"


class RequestStatus(StrEnum):
    """Canonical public request lifecycle statuses."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PaginationInfo(BaseModel):
    """Pagination metadata."""

    total: int
    limit: int
    offset: int
    has_more: bool = Field(serialization_alias="hasMore")


API_CONTRACT_VERSION = "1.0.0"
"""Mobile API contract semver. Bump on every breaking change to the response
shape, request shape, or routing surface (path/method). Coordinates with
docs/openapi/mobile_api.yaml `info.version`. Mobile/CLI clients read this
from the success envelope's meta.api_version to pin against a known
contract."""

MIN_SUPPORTED_CLIENT_API_VERSION = "1.0.0"
"""Oldest client API contract major/minor/patch accepted by this backend."""

API_CAPABILITIES = (
    "auth.credentials",
    "auth.secret",
    "auth.telegram",
    "sync.v1",
    "summaries.v1",
    "collections.v1",
    "search.v1",
)


class SystemMetaResponse(BaseModel):
    """Public backend/client compatibility metadata."""

    api_version: str = Field(
        default=API_CONTRACT_VERSION,
        serialization_alias="apiVersion",
        description="Backend API contract semver.",
    )
    app_version: str = Field(
        default_factory=lambda: os.getenv("APP_VERSION", "1.0.0"),
        serialization_alias="appVersion",
        description="Deploy/application version.",
    )
    build: str | None = Field(
        default_factory=lambda: os.getenv("APP_BUILD") or None,
        description="Build identifier when supplied by deployment.",
    )
    min_supported_client_api_version: str = Field(
        default=MIN_SUPPORTED_CLIENT_API_VERSION,
        serialization_alias="minSupportedClientApiVersion",
        description="Oldest client API contract version accepted by this backend.",
    )
    capabilities: list[str] = Field(default_factory=lambda: list(API_CAPABILITIES))
    feature_flags: dict[str, bool] = Field(
        default_factory=dict,
        serialization_alias="featureFlags",
        description="Server-side feature gates relevant to clients.",
    )
    deprecated_routes: list[str] = Field(
        default_factory=list,
        serialization_alias="deprecatedRoutes",
        description="Routes that still exist but should no longer be used.",
    )


class MetaInfo(BaseModel):
    """Metadata for all API responses."""

    correlation_id: str = ""
    timestamp: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )
    version: str = Field(default_factory=lambda: os.getenv("APP_VERSION", "1.0.0"))
    api_version: str = Field(
        default=API_CONTRACT_VERSION,
        description=(
            "API contract semver. Distinct from `version` (app/build version that "
            "changes every deploy). Bumps only on breaking contract changes."
        ),
    )
    build: str | None = Field(default_factory=lambda: os.getenv("APP_BUILD") or None)
    pagination: PaginationInfo | None = None
    debug: dict[str, Any] | None = None


class ErrorDetail(BaseModel):
    """Error details aligned to API error envelope."""

    code: str
    error_type: str = Field(default=ErrorType.INTERNAL.value, serialization_alias="errorType")
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None
    correlation_id: str = ""
    retry_after: int | None = None


class SuccessResponse(BaseModel):
    """Standard success response wrapper."""

    success: bool = True
    data: Any
    meta: MetaInfo = Field(default_factory=MetaInfo)


class SystemMetaSuccessResponse(SuccessResponse):
    """Success envelope for public backend metadata."""

    data: SystemMetaResponse


class ErrorResponse(BaseModel):
    """Standard error response wrapper."""

    success: bool = False
    error: ErrorDetail
    meta: MetaInfo = Field(default_factory=MetaInfo)


def _coerce_pagination(pagination: BaseModel | dict[str, Any] | None) -> PaginationInfo | None:
    if pagination is None:
        return None
    if isinstance(pagination, PaginationInfo):
        return pagination
    if isinstance(pagination, BaseModel):
        return PaginationInfo.model_validate(pagination.model_dump())
    return PaginationInfo.model_validate(pagination)


def build_meta(
    *,
    correlation_id: str | None = None,
    pagination: BaseModel | dict[str, Any] | None = None,
    debug: dict[str, Any] | None = None,
    version: str | None = None,
    build: str | None = None,
) -> MetaInfo:
    """Construct meta with sensible defaults and context-aware correlation ID."""
    corr = correlation_id or correlation_id_ctx.get() or ""
    pagination_model = _coerce_pagination(pagination)
    meta_kwargs: dict[str, Any] = {
        "correlation_id": corr,
        "pagination": pagination_model,
        "version": version or os.getenv("APP_VERSION", "1.0.0"),
        "build": build or os.getenv("APP_BUILD") or None,
    }
    if debug:
        meta_kwargs["debug"] = debug
    return MetaInfo(**meta_kwargs)


def success_response(
    data: BaseModel | dict[str, Any],
    *,
    correlation_id: str | None = None,
    pagination: BaseModel | dict[str, Any] | None = None,
    debug: dict[str, Any] | None = None,
    version: str | None = None,
    build: str | None = None,
) -> dict[str, Any]:
    """Helper to build a standardized success response."""
    payload = data.model_dump(by_alias=True) if isinstance(data, BaseModel) else data
    meta = build_meta(
        correlation_id=correlation_id,
        pagination=pagination,
        debug=debug,
        version=version,
        build=build,
    )
    return SuccessResponse(data=payload, meta=meta).model_dump(by_alias=True)


def make_error(
    code: str | ErrorCode,
    message: str,
    *,
    error_type: str | ErrorType | None = None,
    retryable: bool | None = None,
    details: dict[str, Any] | None = None,
    retry_after: int | None = None,
) -> ErrorDetail:
    """Create an ErrorDetail with proper typing and defaults."""
    code_str = code.value if isinstance(code, ErrorCode) else code

    if error_type is None:
        if code_str.startswith("AUTH_"):
            error_type = ErrorType.AUTHENTICATION
        elif code_str.startswith("AUTHZ_"):
            error_type = ErrorType.AUTHORIZATION
        elif code_str.startswith("VALIDATION_"):
            error_type = ErrorType.VALIDATION
        elif code_str.startswith("RESOURCE_NOT_FOUND"):
            error_type = ErrorType.NOT_FOUND
        elif code_str.startswith("RESOURCE_"):
            error_type = ErrorType.CONFLICT
        elif code_str.startswith("RATE_LIMIT"):
            error_type = ErrorType.RATE_LIMIT
        elif code_str.startswith("EXTERNAL_"):
            error_type = ErrorType.EXTERNAL_SERVICE
        else:
            error_type = ErrorType.INTERNAL

    error_type_str = error_type.value if isinstance(error_type, ErrorType) else error_type

    if retryable is None:
        retryable = error_type_str in (
            ErrorType.RATE_LIMIT.value,
            ErrorType.EXTERNAL_SERVICE.value,
        ) or code_str in (
            ErrorCode.AUTH_SESSION_EXPIRED.value,
            ErrorCode.EXTERNAL_SERVICE_TIMEOUT.value,
            ErrorCode.EXTERNAL_SERVICE_UNAVAILABLE.value,
        )

    return ErrorDetail(
        code=code_str,
        error_type=error_type_str,
        message=message,
        retryable=retryable,
        details=details,
        retry_after=retry_after,
    )


def _ensure_error_detail(detail: ErrorDetail, correlation_id: str) -> ErrorDetail:
    if detail.correlation_id:
        return detail
    detail_payload = detail.model_dump(by_alias=True)
    detail_payload["correlation_id"] = correlation_id
    return ErrorDetail(**detail_payload)


def error_response(
    detail: ErrorDetail,
    *,
    correlation_id: str | None = None,
    debug: dict[str, Any] | None = None,
    version: str | None = None,
    build: str | None = None,
) -> dict[str, Any]:
    """Helper to build a standardized error response."""
    corr = correlation_id or correlation_id_ctx.get() or ""
    normalized_detail = _ensure_error_detail(detail, corr)
    meta = build_meta(correlation_id=corr, debug=debug, version=version, build=build)
    return ErrorResponse(error=normalized_detail, meta=meta).model_dump(by_alias=True)
