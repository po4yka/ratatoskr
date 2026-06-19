"""Outbound email delivery configuration."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


EmailProvider = Literal["none", "smtp", "resend"]


class EmailConfig(BaseModel):
    """Configuration for optional outbound email delivery sinks."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    provider: EmailProvider = Field(default="none", validation_alias="EMAIL_PROVIDER")
    from_address: str | None = Field(default=None, validation_alias="EMAIL_FROM_ADDRESS")
    from_name: str = Field(default="Ratatoskr", validation_alias="EMAIL_FROM_NAME")
    verification_base_url: str | None = Field(
        default=None,
        validation_alias="EMAIL_VERIFICATION_BASE_URL",
        description="Public URL used to build email verification links.",
    )
    timeout_seconds: float = Field(default=10.0, validation_alias="EMAIL_TIMEOUT_SECONDS")
    resend_api_key: str | None = Field(default=None, validation_alias="RESEND_API_KEY")
    resend_api_url: str = Field(
        default="https://api.resend.com/emails",
        validation_alias="RESEND_API_URL",
    )
    smtp_host: str | None = Field(default=None, validation_alias="SMTP_HOST")
    smtp_port: int = Field(default=587, validation_alias="SMTP_PORT")
    smtp_username: str | None = Field(default=None, validation_alias="SMTP_USERNAME")
    smtp_password: str | None = Field(default=None, validation_alias="SMTP_PASSWORD")
    smtp_use_tls: bool = Field(default=True, validation_alias="SMTP_USE_TLS")

    @field_validator("provider", mode="before")
    @classmethod
    def _validate_provider(cls, value: Any) -> EmailProvider:
        provider = str(value or "none").strip().lower()
        if provider not in {"none", "smtp", "resend"}:
            msg = "EMAIL_PROVIDER must be one of: none, smtp, resend"
            raise ValueError(msg)
        return provider  # type: ignore[return-value]

    @field_validator(
        "from_address",
        "verification_base_url",
        "resend_api_key",
        "smtp_host",
        "smtp_username",
        "smtp_password",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _validate_timeout(cls, value: Any) -> float:
        if value in (None, ""):
            return 10.0
        try:
            parsed = float(str(value))
        except ValueError as exc:
            msg = "EMAIL_TIMEOUT_SECONDS must be a number"
            raise ValueError(msg) from exc
        if parsed <= 0 or parsed > 60:
            msg = "EMAIL_TIMEOUT_SECONDS must be between 0 and 60"
            raise ValueError(msg)
        return parsed

    @field_validator("smtp_port", mode="before")
    @classmethod
    def _validate_smtp_port(cls, value: Any) -> int:
        if value in (None, ""):
            return 587
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "SMTP_PORT must be an integer"
            raise ValueError(msg) from exc
        if parsed < 1 or parsed > 65535:
            msg = "SMTP_PORT must be between 1 and 65535"
            raise ValueError(msg)
        return parsed

    @field_validator("from_name", mode="before")
    @classmethod
    def _validate_from_name(cls, value: Any) -> str:
        name = str(value or "Ratatoskr").strip()
        return name or "Ratatoskr"
