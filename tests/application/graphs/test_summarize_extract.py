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

from app.application.graphs.summarize.deps import SummarizeDeps
from app.application.graphs.summarize.nodes import extract
from app.application.ports.extraction import ExtractionRequest, ExtractionResult


class _FakeExtraction:
    def __init__(self, result: ExtractionResult) -> None:
        self.result = result
        self.calls: list[ExtractionRequest] = []

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        self.calls.append(request)
        return self.result


def _deps(extraction: Any) -> SummarizeDeps:
    m = MagicMock()
    return SummarizeDeps(
        llm_client=m,
        retrieval=m,
        extraction=extraction,
        stream_sink=m,
        summaries=m,
        requests=m,
        summary_index=m,
    )


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
    # Uniform, serializable, id-based delta for every source kind.
    assert set(out) == {"source_text", "content_source", "detected_lang", "dedupe_hash", "title"}
    assert out["source_text"] == f"content for {kind}"
    assert out["content_source"] == source
    assert json.loads(json.dumps(out)) == out
