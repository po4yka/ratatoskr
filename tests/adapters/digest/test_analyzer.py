import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.digest import analyzer as analyzer_module
from app.adapters.digest.analyzer import ANALYSIS_FAILED_STATUS, DigestAnalyzer
from app.config import AppConfig
from app.infrastructure.persistence.digest_store import DigestStore


class _Parsed:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, object]:
        return self._payload


class _Result:
    def __init__(self, payload: dict[str, object]) -> None:
        self.parsed = _Parsed(payload)


class _Cfg(AppConfig):
    def __init__(self) -> None:
        object.__setattr__(self, "digest", SimpleNamespace(concurrency=2))


class _FakeStore(DigestStore):
    def __init__(self, cached: dict[str, object] | None = None) -> None:
        self.cached = cached
        self.persisted: list[tuple[dict[str, object], dict[str, object]]] = []

    async def async_find_cached_analysis(self, post: dict[str, object]) -> dict[str, Any] | None:
        return self.cached

    async def async_persist_analysis(
        self, post: dict[str, object], fields: dict[str, object]
    ) -> None:
        self.persisted.append((post, fields))


class _FakeLLM:
    def __init__(
        self, payload: dict[str, object] | None = None, exc: Exception | None = None
    ) -> None:
        self.payload = payload or {
            "real_topic": "Topic",
            "tldr": "Short summary",
            "key_insights": ["one"],
            "relevance_score": "2",
            "content_type": "invalid",
            "is_ad": 1,
        }
        self.exc = exc
        self.calls = 0

    @property
    def provider_name(self) -> str:
        return "fake"

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        raise NotImplementedError

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        *,
        response_model: type[Any],
        max_retries: int = 3,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        request_id: int | None = None,
        model_override: str | None = None,
        fallback_models_override: tuple[str, ...] | list[str] | None = None,
    ) -> Any:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return _Result(self.payload)

    async def aclose(self) -> None:
        return None


class _UsageResult:
    """chat_structured result carrying token/cost usage metadata."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.parsed = _Parsed(payload)
        self.tokens_prompt = 210
        self.tokens_completion = 55
        self.cost_usd = 0.0009
        self.model_used = "openrouter/digest-model"
        self.latency_ms = 12


class _UsageLLM(_FakeLLM):
    """Fake LLM whose structured result exposes usage metadata."""

    _model = "openrouter/digest-model"

    async def chat_structured(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return _UsageResult(self.payload)


def _analyzer(llm: _FakeLLM, store: _FakeStore | None = None) -> DigestAnalyzer:
    subject = DigestAnalyzer(_Cfg(), llm)
    subject._store = store or _FakeStore()
    return subject


def test_parse_and_validate_llm_response_normalizes_fields() -> None:
    parsed = DigestAnalyzer._parse_and_validate_llm_response(
        {
            "real_topic": "  Topic  ",
            "tldr": " Summary ",
            "key_insights": "not-list",
            "relevance_score": "bad",
            "content_type": "video",
            "is_ad": "yes",
        },
        "cid",
    )

    assert parsed == {
        "real_topic": "Topic",
        "tldr": "Summary",
        "key_insights": [],
        "relevance_score": 0.5,
        "content_type": "other",
        "is_ad": True,
    }


def test_parse_and_validate_llm_response_rejects_missing_required_fields() -> None:
    assert DigestAnalyzer._parse_and_validate_llm_response({"real_topic": "Topic"}, "cid") is None
    assert DigestAnalyzer._parse_and_validate_llm_response({"tldr": "Summary"}, "cid") is None


@pytest.mark.asyncio
async def test_analyze_single_uses_cache_without_calling_llm() -> None:
    cached: dict[str, object] = {"real_topic": "cached", "tldr": "cached"}
    llm = _FakeLLM()
    subject = _analyzer(llm, _FakeStore(cached=cached))

    result = await subject._analyze_single({"message_id": 1, "text": "body"}, "cid", "en")

    assert result == cached
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_analyze_single_persists_valid_llm_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        analyzer_module.DigestAnalyzer, "_load_prompt", staticmethod(lambda lang: "{post_text}")
    )
    store = _FakeStore()
    llm = _FakeLLM()
    subject = _analyzer(llm, store)
    post = {"message_id": 1, "text": "x" * 5000, "url": "https://example.test"}

    result = await subject._analyze_single(post, "cid", "ru")

    assert result is not None
    assert result["real_topic"] == "Topic"
    assert result["relevance_score"] == 1.0
    assert result["content_type"] == "other"
    assert store.persisted[0][0] == post


@pytest.mark.asyncio
async def test_analyze_single_persists_llm_call_metadata_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: the underlying LLM call's cost/tokens/model must be recorded,
    # not just the parsed analysis.
    monkeypatch.setattr(
        analyzer_module.DigestAnalyzer, "_load_prompt", staticmethod(lambda lang: "{post_text}")
    )
    repo = MagicMock()
    repo.async_insert_llm_call = AsyncMock()
    llm = _UsageLLM(
        payload={
            "real_topic": "Topic",
            "tldr": "Short",
            "key_insights": ["a"],
            "relevance_score": "1",
            "content_type": "news",
            "is_ad": False,
        }
    )
    subject = DigestAnalyzer(_Cfg(), llm, llm_repo=repo)
    subject._store = _FakeStore()

    result = await subject._analyze_single(
        {"message_id": 1, "text": "body", "url": "https://example.test"}, "cid", "en"
    )

    assert result is not None
    repo.async_insert_llm_call.assert_awaited_once()
    payload = repo.async_insert_llm_call.await_args.args[0]
    assert payload["status"] == "success"
    assert payload["endpoint"] == "digest_analysis"
    assert payload["request_id"] is None
    assert payload["tokens_prompt"] == 210
    assert payload["tokens_completion"] == 55
    assert payload["cost_usd"] == 0.0009
    assert payload["model"] == "openrouter/digest-model"


@pytest.mark.asyncio
async def test_analyze_single_persists_llm_call_metadata_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An errored LLM call is still recorded (with status=error) so the failure
    # is queryable rather than silently discarded.
    monkeypatch.setattr(
        analyzer_module.DigestAnalyzer, "_load_prompt", staticmethod(lambda lang: "{post_text}")
    )
    repo = MagicMock()
    repo.async_insert_llm_call = AsyncMock()
    llm = _UsageLLM(exc=RuntimeError("provider down"))
    subject = DigestAnalyzer(_Cfg(), llm, llm_repo=repo)
    subject._store = _FakeStore()

    result = await subject._analyze_single(
        {"message_id": 1, "text": "body", "title": "t"}, "cid", "en"
    )

    assert result["analysis_status"] == ANALYSIS_FAILED_STATUS
    repo.async_insert_llm_call.assert_awaited_once()
    payload = repo.async_insert_llm_call.await_args.args[0]
    assert payload["status"] == "error"
    assert payload["endpoint"] == "digest_analysis"
    assert payload["model"] == "openrouter/digest-model"
    assert "provider down" in payload["error_text"]


@pytest.mark.asyncio
async def test_analyze_posts_skips_failed_items(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_analyze_single(
        self: DigestAnalyzer, post: dict[str, object], correlation_id: str, lang: str
    ) -> dict[str, object]:
        if post["message_id"] == 2:
            raise RuntimeError("failed")
        return {"message_id": post["message_id"], "real_topic": "topic"}

    monkeypatch.setattr(DigestAnalyzer, "_analyze_single", fake_analyze_single)
    subject = _analyzer(_FakeLLM())

    result = await subject.analyze_posts(
        [{"message_id": 1, "url": "one"}, {"message_id": 2, "url": "two"}],
        "cid",
    )

    assert result == [{"message_id": 1, "real_topic": "topic"}]


@pytest.mark.asyncio
async def test_analyze_single_returns_fallback_stub_on_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        analyzer_module.DigestAnalyzer, "_load_prompt", staticmethod(lambda lang: "{post_text}")
    )
    subject = _analyzer(_FakeLLM(exc=RuntimeError("down")))
    post = {
        "message_id": 1,
        "text": "Important post body that should still be shown when analysis fails.",
        "title": "Fallback title",
    }

    result = await subject._analyze_single(post, "cid", "en")

    assert result is not None
    assert result["message_id"] == 1
    assert result["real_topic"] == "Fallback title"
    assert result["tldr"] == "Important post body that should still be shown when analysis fails."
    assert result["content_type"] == "other"
    assert result["relevance_score"] == 0.5
    assert result["analysis_status"] == ANALYSIS_FAILED_STATUS


@pytest.mark.asyncio
async def test_analyze_single_marks_schema_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        analyzer_module.DigestAnalyzer, "_load_prompt", staticmethod(lambda lang: "{post_text}")
    )
    store = _FakeStore()
    subject = _analyzer(
        _FakeLLM(payload={"real_topic": "", "tldr": "", "key_insights": []}),
        store,
    )

    result = await subject._analyze_single(
        {"message_id": 1, "text": "Body without valid structured output."},
        "cid",
        "en",
    )

    assert result is not None
    assert result["analysis_status"] == ANALYSIS_FAILED_STATUS
    assert result["real_topic"] == "Body without valid structured output."
    assert store.persisted == []


class _ConcurrencyTrackingStore(DigestStore):
    """Cache store that records the peak number of concurrent cache lookups."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.persisted: list[tuple[dict[str, object], dict[str, object]]] = []

    async def async_find_cached_analysis(self, post: dict[str, object]) -> dict[str, object]:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # Yield enough that all concurrently-admitted lookups overlap before
            # any returns; if the lookup ran outside the semaphore, every post
            # would be in flight at once.
            await asyncio.sleep(0.02)
        finally:
            self.in_flight -= 1
        # Return a cache hit so no LLM call is made.
        return {**post, "real_topic": "cached", "tldr": "cached"}

    async def async_persist_analysis(
        self, post: dict[str, object], fields: dict[str, object]
    ) -> None:
        self.persisted.append((post, fields))


@pytest.mark.asyncio
async def test_cache_lookup_is_bounded_by_concurrency_semaphore() -> None:
    store = _ConcurrencyTrackingStore()
    subject = _analyzer(_FakeLLM())
    subject._store = store

    posts = [{"message_id": i, "url": str(i), "text": "body"} for i in range(6)]
    results = await subject.analyze_posts(posts, "cid")

    assert len(results) == 6
    # concurrency=2 (see _analyzer); the cache lookup now runs inside the
    # semaphore, so no more than 2 lookups are ever in flight at once.
    assert store.max_in_flight == 2
