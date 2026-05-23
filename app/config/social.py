"""Social provider API configuration."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_DEFAULT_THREADS_SCOPES = ("threads_basic",)


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

    @field_validator("threads_scopes", mode="before")
    @classmethod
    def _parse_threads_scopes(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return list(_DEFAULT_THREADS_SCOPES)
        if isinstance(value, str):
            raw_scopes = value.replace(",", " ").split()
        elif isinstance(value, list):
            raw_scopes = [str(item) for item in value]
        else:
            msg = "THREADS_SCOPES must be a comma- or space-separated scope list"
            raise ValueError(msg)

        scopes: list[str] = []
        for raw_scope in raw_scopes:
            scope = raw_scope.strip()
            if scope and scope not in scopes:
                scopes.append(scope)
        return scopes or list(_DEFAULT_THREADS_SCOPES)

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

    @field_validator("threads_graph_base_url", mode="before")
    @classmethod
    def _normalize_threads_graph_base_url(cls, value: Any) -> str:
        base_url = str(value or "https://graph.threads.net/v1.0").strip().rstrip("/")
        if not base_url:
            msg = "THREADS_GRAPH_BASE_URL must not be empty"
            raise ValueError(msg)
        return base_url
