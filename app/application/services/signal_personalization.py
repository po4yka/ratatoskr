"""Vector-store personalization helpers for signal scoring."""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging_utils import get_logger

logger = get_logger(__name__)


class SignalPersonalizationService:
    """Maintain signal-scoring personalization vectors in the vector store."""

    def __init__(
        self,
        *,
        vector_store: Any,
        embedding_service: Any,
    ) -> None:
        self._vector_store = vector_store
        self._embedding_service = embedding_service

    def is_ready(self) -> bool:
        health_check = getattr(self._vector_store, "health_check", None)
        if callable(health_check):
            return bool(health_check())
        return bool(getattr(self._vector_store, "available", False))

    async def embed_topic(
        self,
        *,
        user_id: int,
        topic_id: int,
        name: str,
        description: str | None,
        weight: float,
    ) -> str | None:
        """Generate and upsert a user topic embedding into the vector store."""
        if not self.is_ready():
            return None

        text = self._topic_text(name=name, description=description, weight=weight)
        embedding = await self._embedding_service.generate_embedding(text, task_type="document")
        embedding_ref = f"topic:{int(user_id)}:{int(topic_id)}"
        metadata = {
            "request_id": 0,
            "summary_id": 0,
            "user_id": int(user_id),
            "user_scope": getattr(self._vector_store, "user_scope", "local"),
            "environment": getattr(self._vector_store, "environment", "local"),
            "text": text,
            "title": name,
            "tags": ["signal-topic"],
            "semantic_boosters": [name],
            "local_summary": description,
        }
        try:
            acknowledged = await asyncio.to_thread(
                self._vector_store.upsert_notes,
                [list(embedding)],
                [metadata],
                [embedding_ref],
            )
        except Exception:
            logger.warning(
                "signal_topic_embedding_upsert_failed",
                extra={"user_id": user_id, "topic_id": topic_id},
                exc_info=True,
            )
            return None
        if acknowledged is not True:
            logger.warning(
                "signal_topic_embedding_upsert_unacknowledged",
                extra={"user_id": user_id, "topic_id": topic_id},
            )
            return None
        return embedding_ref

    async def embed_liked_feed_item(
        self,
        *,
        user_id: int,
        feed_item_id: int,
        title: str | None,
        content_text: str | None,
        canonical_url: str | None,
    ) -> str | None:
        """Generate and upsert a liked feed item as future personalization signal."""
        if not self.is_ready():
            return None

        text = self._feed_item_text(
            title=title,
            content_text=content_text,
            canonical_url=canonical_url,
        )
        if not text:
            return None
        embedding = await self._embedding_service.generate_embedding(text, task_type="document")
        embedding_ref = f"liked-feed-item:{int(user_id)}:{int(feed_item_id)}"
        metadata = {
            "request_id": 0,
            "summary_id": 0,
            "user_id": int(user_id),
            "user_scope": getattr(self._vector_store, "user_scope", "local"),
            "environment": getattr(self._vector_store, "environment", "local"),
            "text": text,
            "title": title,
            "url": canonical_url,
            "tags": ["signal-liked-item"],
            "semantic_boosters": [title] if title else [],
            "local_summary": content_text[:500] if content_text else None,
        }
        try:
            acknowledged = await asyncio.to_thread(
                self._vector_store.upsert_notes,
                [list(embedding)],
                [metadata],
                [embedding_ref],
            )
        except Exception:
            logger.warning(
                "signal_liked_item_embedding_upsert_failed",
                extra={"user_id": user_id, "feed_item_id": feed_item_id},
                exc_info=True,
            )
            return None
        if acknowledged is not True:
            logger.warning(
                "signal_liked_item_embedding_upsert_unacknowledged",
                extra={"user_id": user_id, "feed_item_id": feed_item_id},
            )
            return None
        return embedding_ref

    @staticmethod
    def _topic_text(*, name: str, description: str | None, weight: float) -> str:
        parts = [f"Signal topic: {name.strip()}"]
        if description and description.strip():
            parts.append(description.strip())
        parts.append(f"Preference weight: {weight:g}")
        return "\n".join(parts)

    @staticmethod
    def _feed_item_text(
        *,
        title: str | None,
        content_text: str | None,
        canonical_url: str | None,
    ) -> str:
        return "\n".join(
            part.strip()
            for part in (title or "", content_text or "", canonical_url or "")
            if part and part.strip()
        )
