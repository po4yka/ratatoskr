from __future__ import annotations

import pytest

from app.application.services.signal_personalization import SignalPersonalizationService


class _FakeEmbeddingService:
    async def generate_embedding(self, text: str, **kwargs):
        self.text = text
        self.kwargs = kwargs
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    available = True
    environment = "test"
    user_scope = "local"

    def __init__(self) -> None:
        self.upserts: list[dict] = []

    def health_check(self) -> bool:
        return True

    def upsert_notes(self, vectors, metadatas, ids=None):
        self.upserts.append({"vectors": vectors, "metadatas": metadatas, "ids": ids})
        return True


def test_signal_personalization_reports_unready_when_vector_store_is_down() -> None:
    vector_store = _FakeVectorStore()
    vector_store.available = False
    vector_store.health_check = lambda: False  # type: ignore[method-assign]
    service = SignalPersonalizationService(
        vector_store=vector_store,
        embedding_service=_FakeEmbeddingService(),
    )

    assert service.is_ready() is False


@pytest.mark.asyncio
async def test_signal_personalization_embeds_topic_into_vector_store() -> None:
    vector_store = _FakeVectorStore()
    embedding_service = _FakeEmbeddingService()
    service = SignalPersonalizationService(
        vector_store=vector_store,
        embedding_service=embedding_service,
    )

    ref = await service.embed_topic(
        user_id=42,
        topic_id=7,
        name="Distributed systems",
        description="Consensus, storage engines, and reliability",
        weight=1.5,
    )

    assert ref == "topic:42:7"
    assert "Distributed systems" in embedding_service.text
    assert vector_store.upserts[0]["ids"] == ["topic:42:7"]
    assert vector_store.upserts[0]["vectors"] == [[0.1, 0.2, 0.3]]
    assert vector_store.upserts[0]["metadatas"][0]["user_id"] == 42
    assert vector_store.upserts[0]["metadatas"][0]["summary_id"] == 0
    assert vector_store.upserts[0]["metadatas"][0]["tags"] == ["signal-topic"]


@pytest.mark.asyncio
async def test_signal_personalization_embeds_liked_feed_item_into_vector_store() -> None:
    vector_store = _FakeVectorStore()
    service = SignalPersonalizationService(
        vector_store=vector_store,
        embedding_service=_FakeEmbeddingService(),
    )

    ref = await service.embed_liked_feed_item(
        user_id=42,
        feed_item_id=99,
        title="Useful article",
        content_text="A detailed post about storage engines.",
        canonical_url="https://example.com/useful",
    )

    assert ref == "liked-feed-item:42:99"
    assert vector_store.upserts[0]["ids"] == ["liked-feed-item:42:99"]
    metadata = vector_store.upserts[0]["metadatas"][0]
    assert metadata["user_id"] == 42
    assert metadata["summary_id"] == 0
    assert metadata["url"] == "https://example.com/useful"
    assert metadata["tags"] == ["signal-liked-item"]
