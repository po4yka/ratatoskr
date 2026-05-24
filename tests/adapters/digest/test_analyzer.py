from types import SimpleNamespace

import pytest

from app.adapters.digest import analyzer as analyzer_module
from app.adapters.digest.analyzer import DigestAnalyzer


class _Parsed:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, object]:
        return self._payload


class _Result:
    def __init__(self, payload: dict[str, object]) -> None:
        self.parsed = _Parsed(payload)


class _FakeStore:
    def __init__(self, cached: dict[str, object] | None = None) -> None:
        self.cached = cached
        self.persisted: list[tuple[dict[str, object], dict[str, object]]] = []

    async def async_find_cached_analysis(self, post: dict[str, object]) -> dict[str, object] | None:
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

    async def chat_structured(self, *args: object, **kwargs: object) -> _Result:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return _Result(self.payload)


def _analyzer(llm: _FakeLLM, store: _FakeStore | None = None) -> DigestAnalyzer:
    cfg = SimpleNamespace(digest=SimpleNamespace(concurrency=2))
    subject = DigestAnalyzer(cfg, llm)  # type: ignore[arg-type]
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
    cached = {"real_topic": "cached", "tldr": "cached"}
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
async def test_analyze_single_returns_none_on_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        analyzer_module.DigestAnalyzer, "_load_prompt", staticmethod(lambda lang: "{post_text}")
    )
    subject = _analyzer(_FakeLLM(exc=RuntimeError("down")))

    result = await subject._analyze_single({"message_id": 1, "text": "body"}, "cid", "en")

    assert result is None
