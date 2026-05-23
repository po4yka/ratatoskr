from __future__ import annotations

from pathlib import Path

from app.db.models.social import SocialFetchAttempt


def test_social_fetch_attempt_model_has_operational_columns() -> None:
    columns = SocialFetchAttempt.__table__.columns

    for name in (
        "source_url",
        "normalized_url",
        "provider_resource_id",
        "http_status",
        "auth_tier",
        "rate_limit_reset_at",
        "correlation_id",
    ):
        assert name in columns
        assert columns[name].nullable is True


def test_social_fetch_attempt_operational_columns_migration_is_nullable() -> None:
    migration = (
        Path(__file__).parents[2]
        / "app/db/alembic/versions/0023_add_social_fetch_attempt_operational_columns.py"
    ).read_text()

    for name in (
        "source_url",
        "normalized_url",
        "provider_resource_id",
        "http_status",
        "auth_tier",
        "rate_limit_reset_at",
        "correlation_id",
    ):
        assert f'"{name}"' in migration
    assert "nullable=True" in migration
    assert "server_default" not in migration
