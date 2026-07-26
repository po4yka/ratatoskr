"""Thread-safe mutable wrapper around frozen AppConfig with on-demand reload support."""

from __future__ import annotations

import os
import threading
from dataclasses import replace as dc_replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.config.adaptive_timeout import AdaptiveTimeoutConfig
    from app.config.api import ApiLimitsConfig, AuthConfig, SyncConfig
    from app.config.background import BackgroundProcessorConfig
    from app.config.circuit_breaker import CircuitBreakerConfig
    from app.config.content import ContentLimitsConfig
    from app.config.database import DatabaseConfig
    from app.config.digest import ChannelDigestConfig
    from app.config.firecrawl import FirecrawlConfig
    from app.config.github import GitHubConfig
    from app.config.integrations import (
        BatchAnalysisConfig,
        EmbeddingConfig,
        McpConfig,
        QdrantConfig,
        WebSearchConfig,
    )
    from app.config.llm import (
        DirectAnthropicConfig,
        DirectOllamaConfig,
        DirectOpenAIConfig,
        LLMUsageBudgetConfig,
        ModelRoutingConfig,
        OpenRouterConfig,
    )
    from app.config.media import AttachmentConfig, YouTubeConfig
    from app.config.otel import OtelConfig, SentryConfig
    from app.config.push import PushNotificationConfig
    from app.config.redis import RedisConfig
    from app.config.rss import RSSConfig
    from app.config.runtime import RuntimeConfig
    from app.config.scraper import ScraperConfig
    from app.config.settings import AppConfig
    from app.config.signal_ingestion import SignalIngestionConfig
    from app.config.telegram import TelegramConfig, TelegramLimitsConfig
    from app.config.tts import ElevenLabsConfig
    from app.config.twitter import TwitterConfig

logger = get_logger(__name__)

_DEFAULT_MODELS_PATH = "config/ratatoskr.yaml"


class ConfigHolder:
    """Thread-safe mutable wrapper around frozen AppConfig.

    Delegates attribute access to the underlying AppConfig so existing code
    using ``cfg.openrouter.model`` continues to work when given a ConfigHolder.
    """

    if TYPE_CHECKING:
        # Typed view of AppConfig fields — __getattr__ provides the runtime values.
        telegram: TelegramConfig
        firecrawl: FirecrawlConfig
        openrouter: OpenRouterConfig
        openai: DirectOpenAIConfig
        anthropic: DirectAnthropicConfig
        ollama: DirectOllamaConfig
        llm_usage_budget: LLMUsageBudgetConfig
        youtube: YouTubeConfig
        attachment: AttachmentConfig
        runtime: RuntimeConfig
        telegram_limits: TelegramLimitsConfig
        database: DatabaseConfig
        content_limits: ContentLimitsConfig
        vector_store: QdrantConfig
        redis: RedisConfig
        api_limits: ApiLimitsConfig
        auth: AuthConfig
        sync: SyncConfig
        background: BackgroundProcessorConfig
        circuit_breaker: CircuitBreakerConfig
        web_search: WebSearchConfig
        adaptive_timeout: AdaptiveTimeoutConfig
        batch_analysis: BatchAnalysisConfig
        twitter: TwitterConfig
        digest: ChannelDigestConfig
        mcp: McpConfig
        scraper: ScraperConfig
        embedding: EmbeddingConfig
        tts: ElevenLabsConfig
        push: PushNotificationConfig
        model_routing: ModelRoutingConfig
        rss: RSSConfig
        signal_ingestion: SignalIngestionConfig
        otel: OtelConfig
        sentry: SentryConfig
        github: GitHubConfig

    def __init__(self, initial: AppConfig) -> None:
        self._cfg: AppConfig = initial
        self._lock = threading.Lock()
        self._listeners: list[Callable[[AppConfig], None]] = []

    @property
    def cfg(self) -> AppConfig:
        return self._cfg

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cfg, name)

    def register_listener(self, listener: Callable[[AppConfig], None]) -> None:
        """Register a callback invoked with the new config after every swap.

        Runtime singletons that froze a config value at construction (e.g. the
        OpenRouterClient's model) register here so a /setmodel hot-reload
        actually reaches them instead of only updating this snapshot.
        """
        with self._lock:
            self._listeners.append(listener)

    def swap(self, new_cfg: AppConfig) -> AppConfig:
        """Atomically replace the config, notify listeners, return the old one."""
        with self._lock:
            old = self._cfg
            self._cfg = new_cfg
            listeners = list(self._listeners)
        # Notify outside the lock so a listener can't deadlock or stall the swap.
        for listener in listeners:
            try:
                listener(new_cfg)
            except Exception:
                logger.exception("config_listener_failed")
        return old


class ConfigReloader:
    """On-demand config reloader for runtime model hot-reload via /setmodel."""

    def __init__(
        self,
        holder: ConfigHolder,
        models_path: str | None = None,
    ) -> None:
        self._holder = holder
        from app.config.config_file import CONFIG_PATH_ENV

        self._models_path = Path(
            models_path or os.environ.get(CONFIG_PATH_ENV, _DEFAULT_MODELS_PATH)
        )

    def reload_now(self) -> bool:
        """Force an immediate reload. Returns True if config changed."""
        return self._try_reload()

    def _try_reload(self) -> bool:
        """Attempt to reload models config. Returns True if changed."""
        from app.config.config_file import load_models_yaml

        new_env = load_models_yaml(self._models_path)
        if not new_env:
            return False

        old_cfg = self._holder.cfg
        changes: dict[str, tuple[str, str]] = {}

        try:
            new_cfg = self._rebuild_model_sections(old_cfg, new_env, changes)
        except Exception:
            logger.exception("config_rebuild_failed")
            raise

        if not changes:
            return False

        self._holder.swap(new_cfg)
        logger.info(
            "models_config_reloaded",
            extra={"changes": {k: {"old": v[0], "new": v[1]} for k, v in changes.items()}},
        )
        return True

    @staticmethod
    def _apply_section_overrides(
        section_name: str,
        field_map: dict[str, tuple[str, Any]],
        new_env: dict[str, str],
        changes: dict[str, tuple[str, str]],
        coerce: Callable[[str, str], Any] | None = None,
    ) -> dict[str, Any]:
        """Apply env overrides for one config section; record diffs in changes."""
        updates: dict[str, Any] = {}
        for env_key, (field, old_val) in field_map.items():
            if env_key in new_env and new_env[env_key] != str(old_val):
                new_val = new_env[env_key]
                changes[f"{section_name}.{field}"] = (str(old_val), new_val)
                updates[field] = coerce(field, new_val) if coerce else new_val
        return updates

    def _rebuild_model_sections(
        self,
        old_cfg: AppConfig,
        new_env: dict[str, str],
        changes: dict[str, tuple[str, str]],
    ) -> AppConfig:
        """Apply model-section overrides from new YAML values and return updated AppConfig."""

        def _coerce_or(field: str, val: str) -> Any:
            if field in ("fallback_models", "flash_fallback_models"):
                return tuple(m.strip() for m in val.split(",") if m.strip())
            return val

        def _coerce_rt(field: str, val: str) -> Any:
            if field == "enabled":
                return val.lower() in ("true", "1", "yes")
            if field == "fallback_models":
                return tuple(m.strip() for m in val.split(",") if m.strip())
            return val

        or_updates = self._apply_section_overrides(
            "openrouter",
            {
                "OPENROUTER_MODEL": ("model", old_cfg.openrouter.model),
                "OPENROUTER_FLASH_MODEL": ("flash_model", old_cfg.openrouter.flash_model),
                "OPENROUTER_FALLBACK_MODELS": (
                    "fallback_models",
                    ",".join(old_cfg.openrouter.fallback_models),
                ),
                "OPENROUTER_FLASH_FALLBACK_MODELS": (
                    "flash_fallback_models",
                    ",".join(old_cfg.openrouter.flash_fallback_models),
                ),
                "OPENROUTER_LONG_CONTEXT_MODEL": (
                    "long_context_model",
                    old_cfg.openrouter.long_context_model or "",
                ),
            },
            new_env,
            changes,
            _coerce_or,
        )
        rt_updates = self._apply_section_overrides(
            "model_routing",
            {
                "MODEL_ROUTING_DEFAULT": ("default_model", old_cfg.model_routing.default_model),
                "MODEL_ROUTING_TECHNICAL": (
                    "technical_model",
                    old_cfg.model_routing.technical_model,
                ),
                "MODEL_ROUTING_SOCIOPOLITICAL": (
                    "sociopolitical_model",
                    old_cfg.model_routing.sociopolitical_model,
                ),
                "MODEL_ROUTING_LONG_CONTEXT": (
                    "long_context_model",
                    old_cfg.model_routing.long_context_model,
                ),
                "MODEL_ROUTING_ENABLED": (
                    "enabled",
                    str(old_cfg.model_routing.enabled).lower(),
                ),
            },
            new_env,
            changes,
            _coerce_rt,
        )

        att_updates: dict[str, Any] = {}
        if "ATTACHMENT_VISION_MODEL" in new_env:
            old_vision = old_cfg.attachment.vision_model
            new_vision = new_env["ATTACHMENT_VISION_MODEL"]
            if new_vision != old_vision:
                changes["attachment.vision_model"] = (old_vision, new_vision)
                att_updates["vision_model"] = new_vision

        app_updates: dict[str, Any] = {}
        if or_updates:
            app_updates["openrouter"] = old_cfg.openrouter.model_copy(update=or_updates)
        if rt_updates:
            app_updates["model_routing"] = old_cfg.model_routing.model_copy(update=rt_updates)
        if att_updates:
            app_updates["attachment"] = old_cfg.attachment.model_copy(update=att_updates)

        return dc_replace(old_cfg, **app_updates) if app_updates else old_cfg
