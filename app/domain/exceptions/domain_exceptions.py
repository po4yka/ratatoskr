"""Domain-specific exceptions.

These exceptions represent business rule violations and domain errors.
They should be caught and handled by the application layer.
"""

from __future__ import annotations

from typing import Any


class DomainException(Exception):
    """Base exception for all domain errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class InvalidRequestError(DomainException):
    """Raised when a request violates business rules."""


class InvalidStateTransitionError(DomainException):
    """Raised when an invalid state transition is attempted."""


class ResourceNotFoundError(DomainException):
    """Raised when a requested resource does not exist."""


class DuplicateResourceError(DomainException):
    """Raised when attempting to create a duplicate resource."""


class ValidationError(DomainException):
    """Raised when domain validation fails."""


class AuthorizationError(DomainException):
    """Raised when a user lacks sufficient permissions."""


class ProcessingError(DomainException):
    """Raised when a processing operation fails."""
