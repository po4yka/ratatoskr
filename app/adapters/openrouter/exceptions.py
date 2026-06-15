"""Custom exceptions for OpenRouter client."""

from __future__ import annotations

from typing import Any


class OpenRouterError(Exception):
    """Base exception for OpenRouter client errors."""

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        attempt: int | None = None,
        request_id: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.attempt = attempt
        self.request_id = request_id
        self.context = context or {}


class ConfigurationError(OpenRouterError):
    """Raised when there's an issue with client configuration."""

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        attempt: int | None = None,
        request_id: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message, model=model, attempt=attempt, request_id=request_id, context=context
        )
        self.context["error_type"] = "configuration"


class ValidationError(OpenRouterError):
    """Raised when request validation fails."""

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        attempt: int | None = None,
        request_id: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message, model=model, attempt=attempt, request_id=request_id, context=context
        )
        self.context["error_type"] = "validation"


class NetworkError(OpenRouterError):
    """Raised when network-related errors occur."""

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        attempt: int | None = None,
        request_id: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message, model=model, attempt=attempt, request_id=request_id, context=context
        )
        self.context["error_type"] = "network"


class ClientError(OpenRouterError):
    """Raised when there are issues with the HTTP client."""

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        attempt: int | None = None,
        request_id: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message, model=model, attempt=attempt, request_id=request_id, context=context
        )
        self.context["error_type"] = "client"
