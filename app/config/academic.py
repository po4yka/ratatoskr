"""Configuration for the academic-paper metadata + open-access fallback.

Off by default. When ``ACADEMIC_METADATA_FALLBACK_ENABLED=true``, the academic
extractor -- instead of failing when a paper's landing page is Cloudflare-gated
and yields neither an abstract nor a PDF -- recovers the paper's abstract
(OpenAlex / Semantic Scholar / Crossref) and/or an open-access PDF (Unpaywall)
over open scholarly APIs and summarizes that instead.

Enabling the fallback REQUIRES a contact email: Unpaywall returns HTTP 422
without one, and OpenAlex / Crossref reward a ``mailto`` with the faster
"polite pool". The ``model_validator`` turns a misconfiguration (enabled but no
email) into a loud startup error rather than a silent runtime failure.
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.core.logging_utils import get_logger

logger = get_logger(__name__)


class AcademicConfig(BaseModel):
    """Settings for the optional academic metadata / open-access fallback."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    metadata_fallback_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("ACADEMIC_METADATA_FALLBACK_ENABLED"),
        description=(
            "Master switch: when a paper's landing page can't be scraped, recover its "
            "abstract / open-access PDF from open scholarly APIs instead of failing."
        ),
    )

    contact_email: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ACADEMIC_CONTACT_EMAIL", "UNPAYWALL_EMAIL"),
        description=(
            "Contact email sent to the scholarly APIs (Unpaywall requires it; "
            "OpenAlex/Crossref use it for the polite pool). REQUIRED when "
            "metadata_fallback_enabled=true."
        ),
    )

    api_timeout_sec: float = Field(
        default=12.0,
        validation_alias=AliasChoices("ACADEMIC_API_TIMEOUT_SEC"),
        description="Per-provider HTTP timeout (seconds) for the scholarly-API calls.",
    )

    @field_validator("contact_email", mode="before")
    @classmethod
    def _normalize_email(cls, value: Any) -> Any:
        """Normalize blank -> None and reject malformed emails.

        A basic shape check (exactly one ``@``, no whitespace/control chars)
        catches typos at config load and prevents header/URL injection, since the
        value is placed verbatim in a ``User-Agent`` header and a query param.
        """
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            return None
        if any(c.isspace() for c in stripped) or stripped.count("@") != 1:
            msg = "ACADEMIC_CONTACT_EMAIL must be a valid email address"
            raise ValueError(msg)
        return stripped

    @field_validator("api_timeout_sec", mode="before")
    @classmethod
    def _parse_timeout(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
        parsed = float(value)
        if parsed <= 0:
            msg = f"ACADEMIC_API_TIMEOUT_SEC must be > 0, got {parsed}"
            raise ValueError(msg)
        return parsed

    @model_validator(mode="after")
    def _require_email_when_enabled(self) -> Self:
        if self.metadata_fallback_enabled and not self.contact_email:
            msg = (
                "ACADEMIC_METADATA_FALLBACK_ENABLED=true requires a contact email. "
                "Set ACADEMIC_CONTACT_EMAIL (or UNPAYWALL_EMAIL): Unpaywall returns "
                "422 without it and OpenAlex/Crossref reward a mailto with the faster "
                "polite pool."
            )
            raise ValueError(msg)
        return self
