"""Tests for model routing resolution."""

from __future__ import annotations

import pytest

from app.config.llm import ModelRoutingConfig, OpenRouterConfig
from app.core.content_classifier import ContentTier
from app.core.model_router import resolve_fallback_models, resolve_model_for_content


@pytest.fixture
def routing_config() -> ModelRoutingConfig:
    return ModelRoutingConfig(
        enabled=True,
        default_model="deepseek/deepseek-v4-flash",
        technical_model="deepseek/deepseek-v4-pro",
        sociopolitical_model="x-ai/grok-4.20-beta",
        long_context_model="qwen/qwen3.5-plus-02-15",
        long_context_threshold_tokens=80000,
    )


@pytest.fixture
def openrouter_config() -> OpenRouterConfig:
    return OpenRouterConfig(
        api_key="test-key",
        model="deepseek/deepseek-v4-flash",
        # Model selection has no code default; supply the required fields.
        fallback_models=(),
        flash_model="qwen/qwen3.6-flash",
        flash_fallback_models=(),
        long_context_model="minimax/minimax-m2",
        # Behavioral tunables have no code default; supply them explicitly.
        temperature=0.2,
        enable_stats=False,
        enable_structured_outputs=True,
        structured_output_mode="json_schema",
        require_parameters=True,
        auto_fallback_structured=True,
        max_response_size_mb=10,
        enable_prompt_caching=True,
        prompt_cache_ttl="ephemeral",
        prompt_cache_ttl_anthropic="1h",
        cache_system_prompt=True,
        cache_large_content_threshold=4096,
        transport_retry_max_attempts=3,
        transport_retry_min_wait_sec=0.5,
        transport_retry_max_wait_sec=5.0,
    )


class TestResolveModelForContent:
    def test_default_tier(
        self,
        routing_config: ModelRoutingConfig,
        openrouter_config: OpenRouterConfig,
    ) -> None:
        result = resolve_model_for_content(
            tier=ContentTier.DEFAULT,
            content_length=1000,
            has_images=False,
            routing_config=routing_config,
            openrouter_config=openrouter_config,
        )
        assert result == "deepseek/deepseek-v4-flash"

    def test_technical_tier(
        self,
        routing_config: ModelRoutingConfig,
        openrouter_config: OpenRouterConfig,
    ) -> None:
        result = resolve_model_for_content(
            tier=ContentTier.TECHNICAL,
            content_length=1000,
            has_images=False,
            routing_config=routing_config,
            openrouter_config=openrouter_config,
        )
        assert result == "deepseek/deepseek-v4-pro"

    def test_sociopolitical_tier(
        self,
        routing_config: ModelRoutingConfig,
        openrouter_config: OpenRouterConfig,
    ) -> None:
        result = resolve_model_for_content(
            tier=ContentTier.SOCIOPOLITICAL,
            content_length=1000,
            has_images=False,
            routing_config=routing_config,
            openrouter_config=openrouter_config,
        )
        assert result == "x-ai/grok-4.20-beta"

    def test_long_context_overrides_tier(
        self,
        routing_config: ModelRoutingConfig,
        openrouter_config: OpenRouterConfig,
    ) -> None:
        """Long context should override content tier selection.

        80000 tokens * 4 chars/token = 320000 chars; use 320004 to exceed threshold
        (320001 // 4 == 80000, which is not > 80000; need 80001*4 = 320004).
        """
        result = resolve_model_for_content(
            tier=ContentTier.TECHNICAL,
            content_length=320004,
            has_images=False,
            routing_config=routing_config,
            openrouter_config=openrouter_config,
        )
        assert result == "qwen/qwen3.5-plus-02-15"

    def test_below_long_context_threshold(
        self,
        routing_config: ModelRoutingConfig,
        openrouter_config: OpenRouterConfig,
    ) -> None:
        """Content below threshold should use tier model, not long context.

        80000 token threshold * 4 = 320000 chars; 319999 is below.
        """
        result = resolve_model_for_content(
            tier=ContentTier.TECHNICAL,
            content_length=319999,
            has_images=False,
            routing_config=routing_config,
            openrouter_config=openrouter_config,
        )
        assert result == "deepseek/deepseek-v4-pro"

    def test_vision_overrides_all(
        self,
        openrouter_config: OpenRouterConfig,
    ) -> None:
        """Vision model should be selected first when has_images=True."""
        config = ModelRoutingConfig(
            enabled=True,
            default_model="deepseek/deepseek-v4-flash",
            technical_model="deepseek/deepseek-v4-pro",
            sociopolitical_model="x-ai/grok-4.20-beta",
            long_context_model="qwen/qwen3.5-plus-02-15",
            vision_model="google/gemini-2.0-flash",
        )
        result = resolve_model_for_content(
            tier=ContentTier.TECHNICAL,
            content_length=500000,  # Would trigger long-context without vision
            has_images=True,
            routing_config=config,
            openrouter_config=openrouter_config,
        )
        assert result == "google/gemini-2.0-flash"

    def test_vision_not_triggered_without_vision_model(
        self,
        routing_config: ModelRoutingConfig,
        openrouter_config: OpenRouterConfig,
    ) -> None:
        """has_images=True without vision_model set should fall through to tier."""
        result = resolve_model_for_content(
            tier=ContentTier.DEFAULT,
            content_length=1000,
            has_images=True,
            routing_config=routing_config,
            openrouter_config=openrouter_config,
        )
        assert result == "deepseek/deepseek-v4-flash"

    def test_quick_model_short_content(
        self,
        openrouter_config: OpenRouterConfig,
    ) -> None:
        """Short content should route to quick model when configured."""
        config = ModelRoutingConfig(
            enabled=True,
            default_model="deepseek/deepseek-v4-flash",
            technical_model="deepseek/deepseek-v4-pro",
            sociopolitical_model="x-ai/grok-4.20-beta",
            long_context_model="qwen/qwen3.5-plus-02-15",
            quick_model="qwen/qwen3.6-flash",
            quick_threshold_tokens=500,
        )
        # 500 tokens * 4 chars = 2000 chars; use 1999 to be at/below threshold
        result = resolve_model_for_content(
            tier=ContentTier.DEFAULT,
            content_length=1999,
            has_images=False,
            routing_config=config,
            openrouter_config=openrouter_config,
        )
        assert result == "qwen/qwen3.6-flash"

    def test_quick_model_not_triggered_without_config(
        self,
        routing_config: ModelRoutingConfig,
        openrouter_config: OpenRouterConfig,
    ) -> None:
        """QUICK routing should not trigger when quick_model is None."""
        result = resolve_model_for_content(
            tier=ContentTier.DEFAULT,
            content_length=100,  # Very short content
            has_images=False,
            routing_config=routing_config,
            openrouter_config=openrouter_config,
        )
        assert result == "deepseek/deepseek-v4-flash"

    def test_quick_not_triggered_for_long_content(
        self,
        openrouter_config: OpenRouterConfig,
    ) -> None:
        """Content above quick_threshold_tokens should not use quick_model."""
        config = ModelRoutingConfig(
            enabled=True,
            default_model="deepseek/deepseek-v4-flash",
            technical_model="deepseek/deepseek-v4-pro",
            sociopolitical_model="x-ai/grok-4.20-beta",
            long_context_model="qwen/qwen3.5-plus-02-15",
            quick_model="qwen/qwen3.6-flash",
            quick_threshold_tokens=500,
        )
        # 500 tokens * 4 chars = 2000 chars; use 2004 to exceed threshold
        # (2001 // 4 == 500 which is still <= 500; need 501*4 = 2004)
        result = resolve_model_for_content(
            tier=ContentTier.DEFAULT,
            content_length=2004,
            has_images=False,
            routing_config=config,
            openrouter_config=openrouter_config,
        )
        assert result == "deepseek/deepseek-v4-flash"


class TestResolveFallbackModels:
    def test_returns_configured_fallbacks(self) -> None:
        config = ModelRoutingConfig(
            enabled=True,
            fallback_models=(
                "deepseek/deepseek-v4-flash",
                "anthropic/claude-opus-4.6",
                "openai/gpt-5.4",
            ),
        )
        result = resolve_fallback_models(config)
        assert result == (
            "deepseek/deepseek-v4-flash",
            "anthropic/claude-opus-4.6",
            "openai/gpt-5.4",
        )

    def test_tier_none_uses_shared_fallbacks(self) -> None:
        config = ModelRoutingConfig(
            enabled=True,
            fallback_models=("deepseek/deepseek-v4-flash", "minimax/minimax-m2"),
        )
        result = resolve_fallback_models(config, tier=None)
        assert result == ("deepseek/deepseek-v4-flash", "minimax/minimax-m2")

    def test_technical_tier_specific_fallbacks(self) -> None:
        """Technical tier should use technical_fallback_models when non-empty."""
        config = ModelRoutingConfig(
            enabled=True,
            fallback_models=("deepseek/deepseek-v4-flash",),
            technical_fallback_models=("deepseek/deepseek-v4-pro", "openai/gpt-5"),
        )
        result = resolve_fallback_models(config, tier=ContentTier.TECHNICAL)
        assert result == ("deepseek/deepseek-v4-pro", "openai/gpt-5")

    def test_sociopolitical_tier_specific_fallbacks(self) -> None:
        """Sociopolitical tier should use sociopolitical_fallback_models when non-empty."""
        config = ModelRoutingConfig(
            enabled=True,
            fallback_models=("deepseek/deepseek-v4-flash",),
            sociopolitical_fallback_models=("x-ai/grok-4.20-beta", "anthropic/claude-opus-4.6"),
        )
        result = resolve_fallback_models(config, tier=ContentTier.SOCIOPOLITICAL)
        assert result == ("x-ai/grok-4.20-beta", "anthropic/claude-opus-4.6")

    def test_default_tier_specific_fallbacks(self) -> None:
        """Default tier should use default_fallback_models when non-empty."""
        config = ModelRoutingConfig(
            enabled=True,
            fallback_models=("deepseek/deepseek-v4-flash",),
            default_fallback_models=("minimax/minimax-m2",),
        )
        result = resolve_fallback_models(config, tier=ContentTier.DEFAULT)
        assert result == ("minimax/minimax-m2",)

    def test_tier_falls_back_to_shared_when_tier_list_empty(self) -> None:
        """When tier-specific list is empty, shared fallbacks are used."""
        config = ModelRoutingConfig(
            enabled=True,
            fallback_models=("deepseek/deepseek-v4-flash", "minimax/minimax-m2"),
            technical_fallback_models=(),  # empty — should fall back to shared
        )
        result = resolve_fallback_models(config, tier=ContentTier.TECHNICAL)
        assert result == ("deepseek/deepseek-v4-flash", "minimax/minimax-m2")
