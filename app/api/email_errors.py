"""Translate email adapter errors into API exceptions."""

from __future__ import annotations

from typing import NoReturn

from app.adapters.email.service import (
    EmailDeliveryError,
    EmailFeatureDisabledError,
    EmailResourceNotFoundError,
    EmailValidationError,
)
from app.api.exceptions import FeatureDisabledError, ResourceNotFoundError, ValidationError


def raise_email_api_error(exc: EmailDeliveryError) -> NoReturn:
    if isinstance(exc, EmailValidationError):
        raise ValidationError(exc.message, details=exc.details) from exc
    if isinstance(exc, EmailResourceNotFoundError):
        raise ResourceNotFoundError(exc.resource_type, exc.resource_id) from exc
    if isinstance(exc, EmailFeatureDisabledError):
        raise FeatureDisabledError(exc.feature) from exc
    raise RuntimeError(exc.message) from exc
