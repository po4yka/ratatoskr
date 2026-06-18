"""ORM contract tests for encrypted social connection models."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Table

from app.db.models import (
    ALL_MODELS,
    SocialAuthState,
    SocialConnection,
    SocialFetchAttempt,
    SocialProvider,
)


def test_social_models_are_registered() -> None:
    assert SocialConnection in ALL_MODELS
    assert SocialAuthState in ALL_MODELS
    assert SocialFetchAttempt in ALL_MODELS


def test_supported_social_providers_include_x_instagram_threads() -> None:
    assert {provider.value for provider in SocialProvider} >= {"x", "instagram", "threads"}


def test_social_connection_required_columns() -> None:
    columns = SocialConnection.__table__.columns

    for name in (
        "user_id",
        "provider",
        "auth_type",
        "provider_user_id",
        "provider_username",
        "encrypted_access_token",
        "encrypted_refresh_token",
        "token_scopes",
        "access_token_expires_at",
        "refresh_token_expires_at",
        "status",
        "metadata_json",
        "created_at",
        "updated_at",
    ):
        assert name in columns

    assert isinstance(columns["encrypted_access_token"].type, sa.LargeBinary)
    assert isinstance(columns["encrypted_refresh_token"].type, sa.LargeBinary)
    assert columns["user_id"].nullable is False
    assert columns["provider"].nullable is False
    assert columns["auth_type"].nullable is False


def test_social_connection_unique_user_provider_constraint() -> None:
    table = SocialConnection.__table__
    assert isinstance(table, Table)
    constraints = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }

    assert "uq_social_connections_user_provider" in constraints
