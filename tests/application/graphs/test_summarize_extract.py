"""T7: extract node -- the ExtractionPort seam + minimal id-based state delta.

CI-safe: no langgraph / DB. The extraction port is a small fake recording the
``ExtractionRequest`` it received, so the node's single-seam contract + the
serializable, primitive-only state delta are verified directly.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.application.graphs.summarize.deps import SummarizeConfig, SummarizeDeps
from app.application.graphs.summarize.nodes import extract
from app.application.ports.extraction import ExtractionRequest, ExtractionResult


class _FakeExtraction:
    def __init__(self, result: ExtractionResult) -> None:
        self.result = result
        self.calls: list[ExtractionRequest] = []

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        self.calls.append(request)
        return self.result


def _deps(extraction: Any, *, config: SummarizeConfig | None = None) -> SummarizeDeps:
    m = MagicMock()
    return SummarizeDeps(
        llm_client=m,
        retrieval=m,
        extraction=extraction,
        stream_sink=m,
        summaries=m,
        requests=m,
        summary_index=m,
        config=config,
    )


def _config(**over: Any) -> SummarizeConfig:
    base: dict[str, Any] = {
        "model": "base-model",
        "temperature": 0.2,
        "structured_output_mode": "json_schema",
        "long_context_threshold_tokens": 1_000_000,
    }
    base.update(over)
    return SummarizeConfig(**base)


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"correlation_id": "cid-1", "request_id": 42, "lang": "en"}
    base.update(over)
    return base


def _result(**over: Any) -> ExtractionResult:
    base: dict[str, Any] = {
        "request_id": 42,
        "content_text": "the extracted body",
        "content_source": "markdown",
        "detected_lang": "en",
        "dedupe_hash": "abc123",
        "title": "A Title",
    }
    base.update(over)
    return ExtractionResult(**base)


async def test_extract_noop_without_url() -> None:
    fake = _FakeExtraction(_result())
    out = await extract(_state(), deps=_deps(fake))
    assert out == {}
    assert fake.calls == []  # no URL -> port never called


async def test_extract_calls_port_once_with_request_fields() -> None:
    fake = _FakeExtraction(_result())
    await extract(_state(input_url="https://example.com/x"), deps=_deps(fake))
    assert len(fake.calls) == 1
    req = fake.calls[0]
    assert req.url == "https://example.com/x"
    assert req.request_id == 42
    assert req.correlation_id == "cid-1"


async def test_extract_writes_minimal_serializable_state_delta() -> None:
    fake = _FakeExtraction(_result(content_text="BODY", content_source="html", title="T2"))
    out = await extract(_state(input_url="https://example.com/x"), deps=_deps(fake))
    assert out == {
        "lang": "en",
        "source_text": "BODY",
        "content_source": "html",
        "detected_lang": "en",
        "dedupe_hash": "abc123",
        "title": "T2",
    }
    # Serializable-primitive invariant (ADR-0011): no live objects leak into state.
    assert json.loads(json.dumps(out)) == out


async def test_extract_propagates_failure_to_terminal_path() -> None:
    class _Raises:
        async def extract(self, request: ExtractionRequest) -> ExtractionResult:
            raise ValueError("Extraction failed: dead")

    with pytest.raises(ValueError, match="Extraction failed"):
        await extract(_state(input_url="https://example.com/x"), deps=_deps(_Raises()))


# --------------------------------------------------------------------------- #
# Per-source-kind dispatch: the ONE port covers every source kind. The node is
# kind-agnostic; the canned result stands in for what each platform extractor /
# the scraper chain would yield for that URL. The state delta must be uniform +
# serializable across all of them (the parity gate's node-level invariant).
# --------------------------------------------------------------------------- #

_SOURCE_KINDS = [
    ("web_article", "https://example.com/article", "markdown"),
    ("youtube_video", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "markdown"),
    ("x_post", "https://x.com/user/status/123", "markdown"),
    ("academic_paper", "https://arxiv.org/abs/2401.00001", "markdown"),
    ("github_repository", "https://github.com/owner/repo", "markdown"),
    ("threads_meta", "https://www.threads.net/@user/post/abc", "markdown"),
    ("instagram_meta", "https://www.instagram.com/p/abc/", "markdown"),
]


@pytest.mark.parametrize(
    ("kind", "url", "source"), _SOURCE_KINDS, ids=[k for k, _, _ in _SOURCE_KINDS]
)
async def test_extract_uniform_state_delta_per_source_kind(
    kind: str, url: str, source: str
) -> None:
    fake = _FakeExtraction(
        _result(content_text=f"content for {kind}", content_source=source, title=kind)
    )
    out = await extract(_state(input_url=url), deps=_deps(fake))

    # One seam, called once, regardless of source kind (no per-kind branching leaks
    # into the node -- dispatch lives inside the single ExtractionPort adapter).
    assert len(fake.calls) == 1
    assert fake.calls[0].url == url
    # Uniform, serializable, id-based delta for every source kind. ``lang`` is the
    # promoted output language (choose_language(preferred, detected)); uniform too.
    assert set(out) == {
        "lang",
        "source_text",
        "content_source",
        "detected_lang",
        "dedupe_hash",
        "title",
    }
    assert out["source_text"] == f"content for {kind}"
    assert out["content_source"] == source
    assert json.loads(json.dumps(out)) == out


# --------------------------------------------------------------------------- #
# Language promotion (audit #3): under the shipped ``preferred_lang: auto`` the
# CONTENT's detected language must win, so non-English content is summarized in
# its own language. Before the fix, extract wrote ``detected_lang`` but never
# promoted it to ``state['lang']`` -- the pre-extraction default ``en`` leaked
# through to build_prompt / summarize / the cache key.
# --------------------------------------------------------------------------- #


async def test_extract_promotes_detected_lang_under_auto_preference() -> None:
    """preferred_lang=auto + Cyrillic content -> state['lang']=='ru' (not en)."""
    fake = _FakeExtraction(_result(content_text="Это статья на русском языке.", detected_lang="ru"))
    out = await extract(
        _state(input_url="https://example.com/ru", lang="auto"),
        deps=_deps(fake, config=_config(preferred_lang="auto")),
    )
    assert out["lang"] == "ru"
    assert out["detected_lang"] == "ru"


async def test_extract_auto_preference_keeps_english_for_english_content() -> None:
    """preferred_lang=auto + English content -> state['lang']=='en'."""
    fake = _FakeExtraction(_result(content_text="An English article.", detected_lang="en"))
    out = await extract(
        _state(input_url="https://example.com/en", lang="auto"),
        deps=_deps(fake, config=_config(preferred_lang="auto")),
    )
    assert out["lang"] == "en"


async def test_extract_forced_preference_pins_output_lang() -> None:
    """A forced en/ru preference overrides the detected language (choose_language)."""
    fake = _FakeExtraction(_result(content_text="Это статья на русском языке.", detected_lang="ru"))
    out = await extract(
        _state(input_url="https://example.com/ru", lang="en"),
        deps=_deps(fake, config=_config(preferred_lang="en")),
    )
    assert out["lang"] == "en"  # preference pins output even for ru content


async def test_extract_falls_back_to_state_lang_without_config() -> None:
    """No config (bare-mock deps) -> the seeded state lang seeds the resolution."""
    fake = _FakeExtraction(_result(content_text="Это статья на русском языке.", detected_lang="ru"))
    out = await extract(
        _state(input_url="https://example.com/ru", lang="auto"),
        deps=_deps(fake, config=None),
    )
    # state['lang']=='auto' -> detected ('ru') wins.
    assert out["lang"] == "ru"
