"""Catalog of credentials that may be managed from the UI.

This module is the single source of truth for *which* secrets the web UI is
allowed to read the presence of, set, and clear. Adding a runtime service
credential is a one-line change here -- deliberately not a Postgres enum, so a
new provider key needs no migration.

Two classes of secret must never appear in the catalog, and
``NEVER_UI_MANAGED`` is asserted disjoint from it at import time so the mistake
fails loudly rather than silently widening the blast radius:

* **Key-encryption keys** (``GITHUB_TOKEN_ENCRYPTION_KEY``,
  ``BACKUP_ENCRYPTION_KEY``). These encrypt the stored credentials themselves.
  Persisting them in the database they protect would make the encryption
  decorative -- one database read would yield both ciphertext and key.
* **Bootstrap secrets** (``JWT_SECRET_KEY``, ``BOT_TOKEN``, ``DATABASE_URL``).
  The API must already be authenticating and serving before it could render a
  form to edit them, so a UI-managed value is unreachable by construction.

Both classes stay in ``.env`` and rotate through
``docs/runbooks/secret-rotation.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    "CATALOG",
    "NEVER_UI_MANAGED",
    "CredentialGroup",
    "CredentialSpec",
    "get_spec",
    "is_ui_managed",
]


class CredentialGroup:
    """Display grouping for the settings UI."""

    LLM = "llm"
    EMBEDDING = "embedding"
    SPEECH = "speech"
    SCRAPING = "scraping"
    STORAGE = "storage"
    NOTIFICATION = "notification"
    BACKUP = "backup"


@dataclass(frozen=True, slots=True)
class CredentialSpec:
    """One UI-manageable credential.

    Attributes:
        key: Environment-variable name, also the stable storage key.
        label: Human-readable name shown in the UI.
        group: ``CredentialGroup`` bucket for display.
        help_url: Where the owner obtains this credential.
    """

    key: str
    label: str
    group: str
    help_url: str | None = None


_SPECS: tuple[CredentialSpec, ...] = (
    # --- LLM providers -----------------------------------------------------
    CredentialSpec(
        "OPENROUTER_API_KEY", "OpenRouter", CredentialGroup.LLM, "https://openrouter.ai/keys"
    ),
    CredentialSpec(
        "OPENAI_API_KEY", "OpenAI", CredentialGroup.LLM, "https://platform.openai.com/api-keys"
    ),
    CredentialSpec(
        "ANTHROPIC_API_KEY", "Anthropic", CredentialGroup.LLM, "https://console.anthropic.com/"
    ),
    CredentialSpec("OLLAMA_API_KEY", "Ollama", CredentialGroup.LLM),
    # --- Embeddings --------------------------------------------------------
    CredentialSpec(
        "GEMINI_API_KEY",
        "Google Gemini",
        CredentialGroup.EMBEDDING,
        "https://aistudio.google.com/apikey",
    ),
    CredentialSpec("VOYAGE_API_KEY", "Voyage AI", CredentialGroup.EMBEDDING),
    # --- Speech ------------------------------------------------------------
    CredentialSpec(
        "ELEVENLABS_API_KEY", "ElevenLabs", CredentialGroup.SPEECH, "https://elevenlabs.io/app/keys"
    ),
    CredentialSpec("STT_API_KEY", "Speech-to-text", CredentialGroup.SPEECH),
    CredentialSpec("TRANSCRIPTION_API_KEY", "Transcription", CredentialGroup.SPEECH),
    # --- Scraping ----------------------------------------------------------
    CredentialSpec(
        "FIRECRAWL_API_KEY",
        "Firecrawl (hosted)",
        CredentialGroup.SCRAPING,
        "https://firecrawl.dev/",
    ),
    CredentialSpec(
        "FIRECRAWL_SELF_HOSTED_API_KEY", "Firecrawl (self-hosted)", CredentialGroup.SCRAPING
    ),
    CredentialSpec("SCRAPER_CRAWL4AI_TOKEN", "Crawl4AI sidecar", CredentialGroup.SCRAPING),
    CredentialSpec("SCRAPER_DEFUDDLE_TOKEN", "Defuddle sidecar", CredentialGroup.SCRAPING),
    # --- Storage / infrastructure services ---------------------------------
    CredentialSpec("QDRANT_API_KEY", "Qdrant", CredentialGroup.STORAGE),
    # --- Notifications -----------------------------------------------------
    CredentialSpec(
        "RESEND_API_KEY", "Resend (email)", CredentialGroup.NOTIFICATION, "https://resend.com/"
    ),
    # --- Backup ------------------------------------------------------------
    CredentialSpec(
        "AI_BACKUP_CLAUDE_COMPLIANCE_KEY", "Anthropic Compliance API", CredentialGroup.BACKUP
    ),
)

CATALOG: MappingProxyType[str, CredentialSpec] = MappingProxyType({s.key: s for s in _SPECS})

# Never storable through the API. See the module docstring for why each class is
# excluded; the assertion below makes an accidental addition a startup failure.
NEVER_UI_MANAGED: frozenset[str] = frozenset(
    {
        # Key-encryption keys -- protect the very rows this feature writes.
        "GITHUB_TOKEN_ENCRYPTION_KEY",
        "GITHUB_TOKEN_PREVIOUS_KEYS",
        "BACKUP_ENCRYPTION_KEY",
        # Bootstrap -- required before the API can serve the editing UI.
        "JWT_SECRET_KEY",
        "BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "DATABASE_URL",
        "METRICS_BEARER_TOKEN",
    }
)

_overlap = NEVER_UI_MANAGED & set(CATALOG)
if _overlap:  # pragma: no cover - import-time guard
    raise RuntimeError(
        f"Credentials cannot be both UI-managed and forbidden: {sorted(_overlap)}. "
        "Key-encryption and bootstrap secrets must stay in .env; see "
        "docs/runbooks/secret-rotation.md."
    )


def is_ui_managed(key: str) -> bool:
    """Return whether *key* may be set or cleared through the API."""
    return key in CATALOG


def get_spec(key: str) -> CredentialSpec | None:
    """Return the spec for *key*, or ``None`` when it is not UI-manageable."""
    return CATALOG.get(key)
