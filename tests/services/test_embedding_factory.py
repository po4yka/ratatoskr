"""Tests for embedding factory."""

from __future__ import annotations

from collections.abc import Generator

import pytest

from app.config.integrations import EmbeddingConfig
from app.infrastructure.embedding.embedding_factory import (
    create_embedding_service,
    reset_embedding_service_cache,
)
from app.infrastructure.embedding.embedding_protocol import EmbeddingServiceProtocol
from app.infrastructure.embedding.embedding_service import EmbeddingService


@pytest.fixture(autouse=True)
def _clear_service_cache() -> Generator[None]:
    """Isolate the process-wide service cache between tests."""
    reset_embedding_service_cache()
    yield
    reset_embedding_service_cache()


class TestCreateEmbeddingService:
    def test_none_config_returns_local(self) -> None:
        svc = create_embedding_service(None)
        assert isinstance(svc, EmbeddingService)
        assert isinstance(svc, EmbeddingServiceProtocol)

    def test_local_provider_returns_local(self) -> None:
        config = EmbeddingConfig(provider="local")
        svc = create_embedding_service(config)
        assert isinstance(svc, EmbeddingService)

    def test_gemini_provider_returns_gemini(self) -> None:
        config = EmbeddingConfig(
            provider="gemini",
            gemini_api_key="test-key",
            gemini_model="gemini-embedding-2-preview",
            gemini_dimensions=768,
        )
        svc = create_embedding_service(config)
        from app.infrastructure.embedding.gemini_embedding_service import GeminiEmbeddingService

        assert isinstance(svc, GeminiEmbeddingService)
        assert isinstance(svc, EmbeddingServiceProtocol)

    def test_voyage_provider_returns_voyage(self) -> None:
        config = EmbeddingConfig(
            provider="voyage",
            voyage_api_key="test-key",
            voyage_model="voyage-3-large",
            voyage_dimensions=1024,
        )
        svc = create_embedding_service(config)
        from app.infrastructure.embedding.voyage_embedding_service import VoyageEmbeddingService

        assert isinstance(svc, VoyageEmbeddingService)
        assert isinstance(svc, EmbeddingServiceProtocol)

    def test_gemini_without_key_raises(self) -> None:
        config = EmbeddingConfig(provider="gemini", gemini_api_key="")
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            create_embedding_service(config)

    def test_voyage_without_key_raises(self) -> None:
        config = EmbeddingConfig(provider="voyage", voyage_api_key="")
        with pytest.raises(ValueError, match="VOYAGE_API_KEY"):
            create_embedding_service(config)

    def test_unknown_provider_raises(self) -> None:
        config = EmbeddingConfig.__new__(EmbeddingConfig)
        object.__setattr__(config, "provider", "unknown")
        with pytest.raises(ValueError, match="Unknown embedding provider"):
            create_embedding_service(config)


class TestServiceCaching:
    def test_local_service_is_process_cached(self) -> None:
        first = create_embedding_service(None)
        second = create_embedding_service(EmbeddingConfig(provider="local"))
        # None and an explicit local config share the same cache key, so the
        # same instance (and its loaded-model cache) is reused -- not rebuilt.
        assert first is second

    def test_gemini_cached_by_signature(self) -> None:
        cfg = EmbeddingConfig(
            provider="gemini",
            gemini_api_key="k",
            gemini_model="gemini-embedding-2-preview",
            gemini_dimensions=768,
        )
        first = create_embedding_service(cfg)
        second = create_embedding_service(
            EmbeddingConfig(
                provider="gemini",
                gemini_api_key="k",
                gemini_model="gemini-embedding-2-preview",
                gemini_dimensions=768,
            )
        )
        assert first is second

    def test_gemini_distinct_dimensions_not_shared(self) -> None:
        a = create_embedding_service(
            EmbeddingConfig(provider="gemini", gemini_api_key="k", gemini_dimensions=768)
        )
        b = create_embedding_service(
            EmbeddingConfig(provider="gemini", gemini_api_key="k", gemini_dimensions=256)
        )
        assert a is not b

    def test_voyage_cached_by_signature(self) -> None:
        cfg = EmbeddingConfig(provider="voyage", voyage_api_key="k", voyage_dimensions=1024)
        first = create_embedding_service(cfg)
        second = create_embedding_service(
            EmbeddingConfig(provider="voyage", voyage_api_key="k", voyage_dimensions=1024)
        )
        third = create_embedding_service(
            EmbeddingConfig(provider="voyage", voyage_api_key="k", voyage_dimensions=512)
        )
        assert first is second
        assert first is not third

    def test_reset_rebuilds_instance(self) -> None:
        first = create_embedding_service(None)
        reset_embedding_service_cache()
        second = create_embedding_service(None)
        assert first is not second


class TestEmbeddingConfig:
    def test_defaults(self) -> None:
        config = EmbeddingConfig()
        assert config.provider == "local"
        assert config.gemini_api_key == ""
        assert config.gemini_model == "gemini-embedding-2-preview"
        assert config.gemini_dimensions == 768
        assert config.voyage_api_key == ""
        assert config.voyage_model == "voyage-3-large"
        assert config.voyage_dimensions == 1024
        assert config.voyage_base_url == "https://api.voyageai.com/v1"
        assert config.max_token_length == 512

    def test_invalid_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="EMBEDDING_PROVIDER"):
            EmbeddingConfig(provider="invalid")

    def test_dimensions_bounds(self) -> None:
        with pytest.raises(ValueError):
            EmbeddingConfig(gemini_dimensions=127)
        with pytest.raises(ValueError):
            EmbeddingConfig(gemini_dimensions=5000)

    def test_supported_gemini_dimensions_include_128(self) -> None:
        config = EmbeddingConfig(gemini_dimensions=128)
        assert config.gemini_dimensions == 128

    def test_voyage_dimensions_supported_set(self) -> None:
        assert EmbeddingConfig(voyage_dimensions=256).voyage_dimensions == 256
        with pytest.raises(ValueError, match="VOYAGE_EMBEDDING_DIMENSIONS"):
            EmbeddingConfig(voyage_dimensions=768)

    def test_max_token_length_bounds(self) -> None:
        with pytest.raises(ValueError):
            EmbeddingConfig(max_token_length=10)
        with pytest.raises(ValueError):
            EmbeddingConfig(max_token_length=10000)
