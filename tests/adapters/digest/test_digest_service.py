from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.digest import digest_service as digest_module
from app.adapters.digest.analyzer import DigestAnalyzer
from app.adapters.digest.channel_reader import ChannelReader
from app.adapters.digest.digest_service import (
    DigestResult,
    DigestService,
    _deduplicate_posts,
    _topic_bucket_keys,
)
from app.adapters.digest.formatter import DigestFormatter
from app.config import AppConfig
from app.infrastructure.persistence.digest_store import DigestStore
from app.observability import metrics


class _Reader(ChannelReader):
    def __init__(
        self, posts: list[dict[str, Any]] | None = None, exc: Exception | None = None
    ) -> None:
        self.posts = posts or []
        self.exc = exc

    async def fetch_posts_for_user(
        self, user_id: int, max_posts: int | None = None
    ) -> list[dict[str, Any]]:
        if self.exc:
            raise self.exc
        return self.posts

    async def fetch_posts_for_channel(
        self, channel: object, user_id: int, max_posts: int | None = None
    ) -> list[dict[str, Any]]:
        if self.exc:
            raise self.exc
        return self.posts


class _Analyzer(DigestAnalyzer):
    def __init__(
        self, analyzed: list[dict[str, Any]] | None = None, exc: Exception | None = None
    ) -> None:
        self.analyzed = analyzed or []
        self.exc = exc

    async def analyze_posts(
        self, posts: list[dict[str, Any]], correlation_id: str, lang: str = "en"
    ) -> list[dict[str, Any]]:
        if self.exc:
            raise self.exc
        return self.analyzed


class _Formatter(DigestFormatter):
    @staticmethod
    def format_digest(
        analyzed: list[dict[str, Any]],
    ) -> list[tuple[str, list[list[dict[str, str]]]]]:
        return [
            (
                f"digest: {len(analyzed)}",
                [[{"text": "Open", "callback_data": "open:1"}]],
            )
        ]


class _Store(DigestStore):
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.deliveries: list[dict[str, Any]] = []

    async def async_create_delivery(self, **kwargs: Any) -> None:
        if self.exc:
            raise self.exc
        self.deliveries.append(kwargs)

    async def async_get_users_with_subscriptions(self) -> list[int]:
        return [1, 2]

    def get_users_with_subscriptions(self) -> list[int]:
        return [3]

    async def async_get_user_preference(self, user_id: int) -> SimpleNamespace:
        return SimpleNamespace(delivery_channel="telegram", email_address_id=None)


class _Sender:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.messages: list[tuple[int, str, Any]] = []

    async def __call__(self, user_id: int, text: str, reply_markup: Any = None) -> None:
        if self.exc:
            raise self.exc
        self.messages.append((user_id, text, reply_markup))


class _Cfg(AppConfig):
    def __init__(self) -> None:
        object.__setattr__(self, "digest", SimpleNamespace(min_relevance_score=0.5))


def _service(
    *,
    reader: _Reader | None = None,
    analyzer: _Analyzer | None = None,
    sender: _Sender | None = None,
    store: _Store | None = None,
) -> tuple[DigestService, _Sender, _Store]:
    sender = sender or _Sender()
    store = store or _Store()
    subject = DigestService(
        _Cfg(),
        reader or _Reader(),
        analyzer or _Analyzer(),
        _Formatter(),
        sender,
    )
    subject._store = store
    return subject, sender, store


def test_deduplicate_posts_pairwise_and_bucketed() -> None:
    posts = [
        {"real_topic": "AI regulation", "relevance_score": 0.7},
        {"real_topic": "AI regulations", "relevance_score": 0.9},
        {"real_topic": "Space launch", "relevance_score": 0.8},
    ]

    result = _deduplicate_posts(posts)

    assert [post["real_topic"] for post in result] == ["AI regulations", "Space launch"]
    assert "" in _topic_bucket_keys("")
    assert "first:ai" in _topic_bucket_keys("ai regulation future")

    large = [{"real_topic": f"topic {i}", "relevance_score": float(i)} for i in range(70)] + [
        {"real_topic": "topic 69", "relevance_score": 100.0}
    ]
    assert len(_deduplicate_posts(large)) < 70


@pytest.mark.asyncio
async def test_generate_digest_handles_fetch_failure_and_empty_posts() -> None:
    subject, _sender, _store = _service(reader=_Reader(exc=RuntimeError("down")))

    result = await subject.generate_digest(10, "cid")

    assert result.errors == ["Fetch failed: down"]

    subject, sender, _store = _service(reader=_Reader(posts=[]))
    result = await subject.generate_digest(10, "cid")

    assert result.messages_sent == 1
    assert sender.messages[0][0] == 10


@pytest.mark.asyncio
async def test_generate_channel_digest_handles_empty_and_fetch_failure() -> None:
    channel = SimpleNamespace(username="news")
    subject, sender, _store = _service(reader=_Reader(posts=[]))

    result = await subject.generate_channel_digest(10, channel, "cid")

    assert result.messages_sent == 1
    assert "@news" in sender.messages[0][1]

    subject, _sender, _store = _service(reader=_Reader(exc=RuntimeError("down")))
    result = await subject.generate_channel_digest(10, channel, "cid")

    assert result.errors == ["Fetch failed: down"]


@pytest.mark.asyncio
async def test_run_digest_pipeline_filters_delivers_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        digest_module, "_build_inline_keyboard", lambda buttons: {"buttons": buttons}
    )
    analyzed = [
        {
            "message_id": 1,
            "real_topic": "Topic one",
            "relevance_score": 0.9,
            "_channel_username": "one",
            "content_type": "news",
        },
        {
            "message_id": 2,
            "real_topic": "Topic one duplicate",
            "relevance_score": 0.8,
            "_channel_username": "two",
            "content_type": "news",
        },
        {
            "message_id": 3,
            "real_topic": "Ad",
            "relevance_score": 1.0,
            "_channel_username": "ads",
            "is_ad": True,
        },
        {
            "message_id": 4,
            "real_topic": "Low relevance",
            "relevance_score": 0.1,
            "_channel_username": "low",
            "content_type": "news",
        },
    ]
    subject, sender, store = _service(
        reader=_Reader(posts=[{"message_id": 1}]),
        analyzer=_Analyzer(analyzed),
    )

    result = await subject.generate_digest(10, "cid", lang="en")

    assert result.post_count == 2
    assert result.channel_count == 2
    assert result.messages_sent == 1
    assert sender.messages[0][2] == {"buttons": [[{"text": "Open", "callback_data": "open:1"}]]}
    assert store.deliveries[0]["post_ids"] == [1, 2]


@pytest.mark.asyncio
async def test_run_digest_pipeline_records_analysis_send_and_persist_errors() -> None:
    subject, _sender, _store = _service(
        reader=_Reader(posts=[{"message_id": 1}]),
        analyzer=_Analyzer(exc=RuntimeError("bad analysis")),
    )
    result = await subject.generate_digest(10, "cid")
    assert result.errors == ["Analysis failed: bad analysis"]

    subject, _sender, _store = _service(
        reader=_Reader(posts=[{"message_id": 1}]),
        analyzer=_Analyzer([{"message_id": 1, "is_ad": True, "content_type": "news"}]),
        sender=_Sender(exc=RuntimeError("send failed")),
    )
    result = await subject.generate_digest(10, "cid")
    assert result.errors == ["Send failed: send failed"]

    subject, sender, _store = _service(
        reader=_Reader(posts=[{"message_id": 1}]),
        analyzer=_Analyzer(
            [
                {
                    "message_id": 1,
                    "real_topic": "Topic",
                    "relevance_score": 0.9,
                    "content_type": "news",
                }
            ]
        ),
        store=_Store(exc=RuntimeError("db failed")),
    )
    result = await subject.generate_digest(10, "cid")
    assert sender.messages
    assert result.errors == ["Delivery record not saved: db failed"]


@pytest.mark.asyncio
@pytest.mark.skipif(not metrics.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
async def test_generate_digest_records_success_path_metrics() -> None:
    registry = metrics.REGISTRY
    assert registry is not None
    before_deliveries = (
        registry.get_sample_value("ratatoskr_digest_deliveries_total", {"status": "sent"}) or 0.0
    )
    before_posts = (
        registry.get_sample_value("ratatoskr_digest_posts_analyzed_total", {"status": "ok"}) or 0.0
    )

    subject, _sender, _store = _service(
        reader=_Reader(posts=[{"message_id": 1}]),
        analyzer=_Analyzer(
            [
                {
                    "message_id": 1,
                    "real_topic": "Topic",
                    "relevance_score": 0.9,
                    "_channel_username": "news",
                    "content_type": "news",
                }
            ]
        ),
    )

    result = await subject.generate_digest(10, "cid", digest_type="scheduled")

    assert result.errors == []
    assert (
        registry.get_sample_value("ratatoskr_digest_deliveries_total", {"status": "sent"}) or 0.0
    ) - before_deliveries == pytest.approx(1.0)
    assert (
        registry.get_sample_value("ratatoskr_digest_posts_analyzed_total", {"status": "ok"}) or 0.0
    ) - before_posts == pytest.approx(1.0)
    assert (
        registry.get_sample_value(
            "ratatoskr_digest_pipeline_duration_seconds_count",
            {"digest_type": "scheduled", "status": "sent"},
        )
        or 0.0
    ) >= 1.0


@pytest.mark.asyncio
@pytest.mark.skipif(not metrics.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
async def test_generate_digest_records_llm_failure_metrics() -> None:
    registry = metrics.REGISTRY
    assert registry is not None
    before_deliveries = (
        registry.get_sample_value("ratatoskr_digest_deliveries_total", {"status": "failed"}) or 0.0
    )
    before_posts = (
        registry.get_sample_value(
            "ratatoskr_digest_posts_analyzed_total",
            {"status": "llm_error"},
        )
        or 0.0
    )

    subject, _sender, _store = _service(
        reader=_Reader(posts=[{"message_id": 1}]),
        analyzer=_Analyzer(exc=RuntimeError("bad analysis")),
    )

    result = await subject.generate_digest(10, "cid")

    assert result.errors == ["Analysis failed: bad analysis"]
    assert (
        registry.get_sample_value("ratatoskr_digest_deliveries_total", {"status": "failed"}) or 0.0
    ) - before_deliveries == pytest.approx(1.0)
    assert (
        registry.get_sample_value(
            "ratatoskr_digest_posts_analyzed_total",
            {"status": "llm_error"},
        )
        or 0.0
    ) - before_posts == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_subscription_helpers_delegate_to_store() -> None:
    subject, _sender, _store = _service()

    assert await subject.async_get_users_with_subscriptions() == [1, 2]
    assert subject.get_users_with_subscriptions() == [3]


def test_digest_result_defaults() -> None:
    result = DigestResult(user_id=1)

    assert result.post_count == 0
    assert result.errors == []
