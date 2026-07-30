"""Model capabilities detection and caching for OpenRouter."""

from __future__ import annotations

import re
import time
from typing import Any, cast

import httpx

from app.core.logging_utils import get_logger

# Provider patterns for detecting which provider a model belongs to
PROVIDER_PATTERNS: dict[str, list[str]] = {
    "anthropic": ["anthropic/", "claude-"],
    "google": ["google/", "gemini-"],
    "openai": ["openai/", "gpt-"],
    "deepseek": ["deepseek/"],
    "qwen": ["qwen/"],
    "minimax": ["minimax/"],
    "moonshotai": ["moonshotai/", "kimi-"],
    "meta": ["meta-llama/", "llama-"],
    "mistral": ["mistral/", "mistral-"],
    "cohere": ["cohere/"],
}

# Providers that require explicit cache_control breakpoints
# - Anthropic: max 4 breakpoints, TTL ephemeral (5min) or 1h
# - Google: only last breakpoint used, min ~4096 tokens
EXPLICIT_CACHING_PROVIDERS: frozenset[str] = frozenset({"anthropic", "google"})

# Providers with automatic/implicit caching (no configuration needed)
# These handle caching server-side without explicit cache_control
LOGGER = get_logger(__name__)

# Anchored regex patterns for reasoning-heavy model detection.
# Plain substring matching (e.g. "o1") produces false positives on model names
# that happen to contain those characters (e.g. "mistral/medio1").
# These patterns require the indicator to sit at a path/name boundary.
_REASONING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|[/_-])o\d+(?:[/_-]|$)"),
    re.compile(r"(?:^|[/_-])-?r\d+(?:[/_-]|$)"),
    re.compile(r"thinking"),
    re.compile(r"reasoning"),
)

AUTOMATIC_CACHING_PROVIDERS: frozenset[str] = frozenset(
    {
        "openai",  # Automatic, 1024 token minimum, free writes, 0.25-0.50x reads
        "deepseek",  # Automatic caching
        "qwen",  # Implicit caching via Alibaba Cloud (supports_implicit_caching: true)
        "moonshotai",  # Automatic caching for Kimi models
        "minimax",  # Automatic caching
    }
)


def detect_provider(model: str) -> str:
    """Detect the provider for a given model name.

    Args:
        model: Model identifier (e.g., "anthropic/claude-3-opus", "qwen/qwen3-max")

    Returns:
        Provider name (e.g., "anthropic", "google", "openai") or "unknown"
    """
    model_lower = model.lower()
    for provider, patterns in PROVIDER_PATTERNS.items():
        for pattern in patterns:
            if pattern.lower() in model_lower:
                return provider
    return "unknown"


def supports_explicit_caching(model: str) -> bool:
    """Check if model's provider requires explicit cache_control breakpoints.

    Args:
        model: Model identifier

    Returns:
        True if the provider (Anthropic/Google) requires explicit cache_control
    """
    provider = detect_provider(model)
    return provider in EXPLICIT_CACHING_PROVIDERS


def supports_automatic_caching(model: str) -> bool:
    """Check if model's provider has automatic caching.

    Args:
        model: Model identifier

    Returns:
        True if the provider handles caching automatically (no config needed)
    """
    provider = detect_provider(model)
    return provider in AUTOMATIC_CACHING_PROVIDERS


def get_caching_info(model: str) -> dict[str, Any]:
    """Get detailed caching information for a model.

    Args:
        model: Model identifier

    Returns:
        Dict with caching details: supports_caching, caching_type, provider, notes
    """
    provider = detect_provider(model)

    if provider in EXPLICIT_CACHING_PROVIDERS:
        notes = {
            "anthropic": "Max 4 breakpoints, TTL: ephemeral (5min) or 1h",
            "google": "Only last breakpoint used, min ~4096 tokens",
        }
        return {
            "supports_caching": True,
            "caching_type": "explicit",
            "provider": provider,
            "requires_cache_control": True,
            "notes": notes.get(provider, "Requires cache_control breakpoints"),
        }

    if provider in AUTOMATIC_CACHING_PROVIDERS:
        notes = {
            "openai": "Automatic, 1024 token min, free writes, 0.25-0.50x reads",
            "deepseek": "Automatic caching, no config needed",
            "qwen": "Implicit caching via Alibaba Cloud",
            "moonshotai": "Automatic caching for Kimi models",
            "minimax": "Automatic caching",
        }
        return {
            "supports_caching": True,
            "caching_type": "automatic",
            "provider": provider,
            "requires_cache_control": False,
            "notes": notes.get(provider, "Automatic caching, no config needed"),
        }

    return {
        "supports_caching": False,
        "caching_type": "unknown",
        "provider": provider,
        "requires_cache_control": False,
        "notes": "Caching support unknown for this provider",
    }


class ModelCapabilities:
    """Handles model capability detection and caching."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        http_referer: str | None = None,
        x_title: str | None = None,
        timeout: int = 60,
        capabilities_ttl_sec: int = 3600,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._http_referer = http_referer
        self._x_title = x_title
        self._timeout = timeout
        self._capabilities_ttl_sec = capabilities_ttl_sec
        self._logger = get_logger(__name__)

        # Cache capabilities: which models support structured outputs
        self._structured_supported_models: set[str] | None = None
        self._structured_models: set[str] | None = None
        self._capabilities_last_load: float = 0.0

        # Known models that support structured outputs (fallback list)
        self._known_structured_models = {
            # DeepSeek models (JSON mode support)
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-pro",
            # Moonshot AI (Kimi) models
            "moonshotai/kimi-k2",
            "moonshotai/kimi-k2-0905",
            "moonshotai/kimi-k2.5",
            # Qwen models
            "qwen/qwen3-max",
            "qwen/qwen3.5-plus-02-15",
            "qwen/qwen3.6-flash",
            "qwen/qwen3.6-plus-04-02",
            "qwen/qwen3-235b-a22b-2507",
            "qwen/qwen3-next-80b-a3b-thinking",
            "qwen/qwen3-coder-plus",
            "qwen/qwen3-coder:free",
            # MiniMax models
            "minimax/minimax-m2",
        }

    def set_api_key(self, api_key: str) -> None:
        """Replace the bearer credential used for capability lookups.

        Headers are built per request, so this takes effect on the next call.
        Mirrors ``RequestBuilder.set_api_key``; without it a rotated key left
        capability probes authenticating with the retired credential.
        """
        self._api_key = api_key

    def is_reasoning_heavy_model(self, model: str) -> bool:
        """Check if model is reasoning-heavy (like Kimi K2 Thinking, OpenAI o1).

        Uses anchored regex patterns to avoid false positives from models whose
        names happen to contain indicator substrings (e.g. ``mistral/medio1``).
        """
        model_lower = model.lower()
        return any(pat.search(model_lower) for pat in _REASONING_PATTERNS)

    def get_safe_structured_fallbacks(self) -> list[str]:
        """Get list of models known to support structured outputs reliably."""
        return [
            "minimax/minimax-m2",  # structured-output capable per live capability probe
            "qwen/qwen3.5-plus-02-15",  # strong structured-output support
            "deepseek/deepseek-v4-flash",  # paid fallback
        ]

    def supports_structured_outputs(self, model: str) -> bool:
        """Check if a model supports structured outputs."""
        # Check cached capabilities first
        if self._structured_supported_models:
            return model in self._structured_supported_models

        # Fallback to known models list
        return model in self._known_structured_models

    async def ensure_structured_supported_models(self) -> None:
        """Fetch and cache models supporting structured outputs."""
        now = time.time()
        if (
            self._structured_supported_models is not None
            and (now - self._capabilities_last_load) < self._capabilities_ttl_sec
        ):
            return

        try:
            models = await self._fetch_structured_models()
            if models:
                self._structured_supported_models = models
                self._structured_models = models
                self._logger.debug(
                    "structured_outputs_capabilities_loaded",
                    extra={"models_count": len(models)},
                )
            # Keep existing cache or use known models as fallback
            elif self._structured_supported_models is None:
                self._structured_supported_models = self._known_structured_models.copy()
                self._structured_models = self._structured_supported_models
                self._logger.warning(
                    "using_fallback_structured_models",
                    extra={"models_count": len(self._structured_supported_models)},
                )

            self._capabilities_last_load = now
        except Exception as e:
            self._capabilities_last_load = now
            # Use known models as fallback
            if self._structured_supported_models is None:
                self._structured_supported_models = self._known_structured_models.copy()
            self._structured_models = self._structured_supported_models

            self._logger.warning(
                "openrouter_capabilities_probe_failed",
                extra={"error": str(e), "using_fallback": True},
            )

    async def get_models(self) -> dict[str, Any]:
        """Get available models from OpenRouter API."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self._http_referer or "https://github.com/your-repo",
            "X-Title": self._x_title or "Ratatoskr Bot",
        }

        try:
            limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
            async with httpx.AsyncClient(timeout=self._timeout, limits=limits) as client:
                resp = await client.get(f"{self._base_url}/models", headers=headers)
                resp.raise_for_status()
                return cast("dict[str, Any]", resp.json())
        except Exception as e:
            self._logger.exception("openrouter_models_error", extra={"error": str(e)})
            raise

    async def get_structured_models(self) -> set[str]:
        """Get set of models that support structured outputs."""
        await self.ensure_structured_supported_models()
        return self._structured_supported_models or set()

    async def _fetch_structured_models(self) -> set[str]:
        """Retrieve structured-capable models from OpenRouter."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self._http_referer or "https://github.com/your-repo",
            "X-Title": self._x_title or "Ratatoskr Bot",
        }

        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        async with httpx.AsyncClient(timeout=self._timeout, limits=limits) as client:
            resp = await client.get(
                f"{self._base_url}/models?supported_parameters=structured_outputs",
                headers=headers,
            )
            resp.raise_for_status()
            payload = resp.json()

        models: set[str] = set()
        data_array = []

        if isinstance(payload, dict):
            if isinstance(payload.get("data"), list):
                data_array = payload.get("data", [])
            elif isinstance(payload.get("models"), list):
                data_array = payload.get("models", [])

        for item in data_array:
            try:
                if isinstance(item, dict):
                    model_id = item.get("id") or item.get("name") or item.get("model")
                    if isinstance(model_id, str) and model_id:
                        models.add(model_id)
            except Exception as exc:
                LOGGER.debug(
                    "openrouter_model_entry_parse_skipped",
                    extra={"error": str(exc)},
                )
                continue

        return models

    def build_model_fallback_list(
        self,
        primary_model: str,
        fallback_models: list[str],
        response_format: dict[str, Any] | None = None,
        enable_structured_outputs: bool = True,
    ) -> list[str]:
        """Build list of models to try, including structured output fallbacks."""
        models_to_try = [primary_model, *fallback_models]

        # Add structured output fallbacks if needed
        if (
            response_format is not None
            and enable_structured_outputs
            and self.is_reasoning_heavy_model(primary_model)
        ):
            safe_models = self.get_safe_structured_fallbacks()
            for safe_model in safe_models:
                if safe_model not in models_to_try:
                    models_to_try.append(safe_model)

        return models_to_try
