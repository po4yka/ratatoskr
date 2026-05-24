"""Social provider API configuration."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ._secret_marker import SECRET_MARKER

_DEFAULT_THREADS_SCOPES = ("threads_basic",)
_DEFAULT_INSTAGRAM_SCOPES = ("instagram_business_basic",)


class SocialConfig(BaseModel):
    """Configuration for connected social account APIs."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    threads_client_id: str | None = Field(
        default=None,
        validation_alias="THREADS_CLIENT_ID",
        description="Threads OAuth client ID",
    )
    threads_client_secret: SecretStr | None = Field(
        default=None,
        validation_alias="THREADS_CLIENT_SECRET",
        description="Threads OAuth client secret",
        json_schema_extra=SECRET_MARKER,
    )
    threads_redirect_uri: str | None = Field(
        default=None,
        validation_alias="THREADS_REDIRECT_URI",
        description="Threads OAuth redirect URI",
    )
    threads_scopes: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_THREADS_SCOPES),
        validation_alias="THREADS_SCOPES",
        description="Read-only Threads OAuth scopes",
    )
    threads_graph_base_url: str = Field(
        default="https://graph.threads.net/v1.0",
        validation_alias="THREADS_GRAPH_BASE_URL",
        description="Threads Graph API base URL",
    )
    instagram_client_id: str | None = Field(
        default=None,
        validation_alias="INSTAGRAM_CLIENT_ID",
        description="Instagram OAuth client ID",
    )
    instagram_client_secret: SecretStr | None = Field(
        default=None,
        validation_alias="INSTAGRAM_CLIENT_SECRET",
        description="Instagram OAuth client secret",
        json_schema_extra=SECRET_MARKER,
    )
    instagram_redirect_uri: str | None = Field(
        default=None,
        validation_alias="INSTAGRAM_REDIRECT_URI",
        description="Instagram OAuth redirect URI",
    )
    instagram_scopes: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_INSTAGRAM_SCOPES),
        validation_alias="INSTAGRAM_SCOPES",
        description="Read-only Instagram OAuth scopes for professional account profile/media reads",
    )
    instagram_graph_base_url: str = Field(
        default="https://graph.instagram.com/v25.0",
        validation_alias="INSTAGRAM_GRAPH_BASE_URL",
        description="Instagram Graph API base URL",
    )

    @field_validator("threads_scopes", mode="before")
    @classmethod
    def _parse_threads_scopes(cls, value: Any) -> list[str]:
        return _parse_scope_list(
            value,
            default_scopes=_DEFAULT_THREADS_SCOPES,
            env_name="THREADS_SCOPES",
        )

    @field_validator("instagram_scopes", mode="before")
    @classmethod
    def _parse_instagram_scopes(cls, value: Any) -> list[str]:
        return _parse_scope_list(
            value,
            default_scopes=_DEFAULT_INSTAGRAM_SCOPES,
            env_name="INSTAGRAM_SCOPES",
        )

    @field_validator("threads_scopes")
    @classmethod
    def _validate_threads_scopes(cls, value: list[str]) -> list[str]:
        disallowed = sorted(
            scope
            for scope in value
            if scope in {"threads_content_publish", "threads_manage_replies"}
        )
        if disallowed:
            msg = f"THREADS_SCOPES must not include publish or reply-management scopes: {', '.join(disallowed)}"
            raise ValueError(msg)
        return value

    @field_validator("instagram_scopes")
    @classmethod
    def _validate_instagram_scopes(cls, value: list[str]) -> list[str]:
        allowed = {"instagram_business_basic"}
        disallowed = sorted(scope for scope in value if scope not in allowed)
        if disallowed:
            msg = f"INSTAGRAM_SCOPES currently supports read-only profile/media scope only: {', '.join(disallowed)}"
            raise ValueError(msg)
        return value

    @field_validator("threads_graph_base_url", mode="before")
    @classmethod
    def _normalize_threads_graph_base_url(cls, value: Any) -> str:
        base_url = str(value or "https://graph.threads.net/v1.0").strip().rstrip("/")
        if not base_url:
            msg = "THREADS_GRAPH_BASE_URL must not be empty"
            raise ValueError(msg)
        return base_url

    @field_validator("instagram_graph_base_url", mode="before")
    @classmethod
    def _normalize_instagram_graph_base_url(cls, value: Any) -> str:
        base_url = str(value or "https://graph.instagram.com/v25.0").strip().rstrip("/")
        if not base_url:
            msg = "INSTAGRAM_GRAPH_BASE_URL must not be empty"
            raise ValueError(msg)
        return base_url


def _parse_scope_list(
    value: Any,
    *,
    default_scopes: tuple[str, ...],
    env_name: str,
) -> list[str]:
    if value in (None, ""):
        return list(default_scopes)
    if isinstance(value, str):
        raw_scopes = value.replace(",", " ").split()
    elif isinstance(value, list):
        raw_scopes = [str(item) for item in value]
    else:
        msg = f"{env_name} must be a comma- or space-separated scope list"
        raise ValueError(msg)

    scopes: list[str] = []
    for raw_scope in raw_scopes:
        scope = raw_scope.strip()
        if scope and scope not in scopes:
            scopes.append(scope)
    return scopes or list(default_scopes)
