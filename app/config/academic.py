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

    browser_pdf_recovery_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("ACADEMIC_BROWSER_PDF_RECOVERY_ENABLED"),
        description=(
            "Tier 1: when the cookie-less httpx PDF download is blocked (paywall / 403 / "
            "non-PDF), re-fetch the deterministic PDF URL through the CloakBrowser stealth "
            "session that already cleared Cloudflare for the landing page. Cheap (one extra "
            "browser render, no LLM). No-ops when the CloakBrowser provider is unavailable."
        ),
    )

    agentic_pdf_download_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("ACADEMIC_AGENTIC_PDF_DOWNLOAD_ENABLED"),
        description=(
            "Tier 2 (opt-in, costs LLM): for hosts with no deterministic PDF URL and no "
            "harvestable anchor, let a single flash-LLM call locate the download control, "
            "click it, and capture the file. Double-gated by agentic_pdf_host_allowlist."
        ),
    )

    agentic_pdf_host_allowlist: tuple[str, ...] = Field(
        default_factory=tuple,
        validation_alias=AliasChoices("ACADEMIC_AGENTIC_PDF_HOST_ALLOWLIST"),
        description=(
            "Hosts where the tier-2 agentic download may fire (CSV or list). Empty means "
            "no host is allowed; required (non-empty) when agentic_pdf_download_enabled=true."
        ),
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

    @field_validator("agentic_pdf_host_allowlist", mode="before")
    @classmethod
    def _parse_host_allowlist(cls, value: Any) -> Any:
        """Accept a CSV string or a list; normalize to a lower-cased host tuple."""
        if value is None:
            return ()
        if isinstance(value, str):
            value = value.split(",")
        if isinstance(value, (list, tuple)):
            return tuple(h.strip().lower() for h in value if str(h).strip())
        return value

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

    @model_validator(mode="after")
    def _require_allowlist_when_agentic_enabled(self) -> Self:
        if self.agentic_pdf_download_enabled and not self.agentic_pdf_host_allowlist:
            msg = (
                "ACADEMIC_AGENTIC_PDF_DOWNLOAD_ENABLED=true requires a non-empty "
                "ACADEMIC_AGENTIC_PDF_HOST_ALLOWLIST. The tier-2 agentic download spends "
                "real LLM budget per paper, so it is double-gated by an explicit host "
                "allowlist (same policy as WEBWRIGHT_HOST_ALLOWLIST)."
            )
            raise ValueError(msg)
        return self
