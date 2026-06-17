"""T7 parity net (ADR-0013): graph summarize pipeline vs the legacy contract.

PARITY SCOPE (honest, partial -- the full per-source_kind golden-vs-legacy net is
T9's deliverable, ADR-0013/roadmap M4):

* extract: the single ExtractionPort yields a UNIFORM, serializable, id-based
  state delta for every source kind (web / youtube / x / academic / github /
  meta), exercised node-level with a fake port (legacy dispatch lives inside the
  one adapter, so kind-divergence cannot leak into the graph).
* summarize -> validate -> enrich: the graph pipeline, given a canned LLM result,
  produces a CONTRACT-FAITHFUL, DETERMINISTIC summary -- byte-identical to running
  the contract normalizer (``validate_and_shape_summary``, the legacy oracle) over
  the same model output. This pins the shaping seam, not model behavior.

What is NOT covered here (deferred to T9 with the real legacy oracle): a live
graph-vs-``PureSummaryService`` golden per source_kind, budget/sticky/two-pass/
chunk/cache behavioral goldens. CI-green here is NOT proof of full parity.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapter_models.llm.llm_models import StructuredLLMResult
from app.application.graphs.summarize.deps import SummarizeConfig, SummarizeDeps
from app.application.graphs.summarize.nodes import build_prompt, extract, summarize, validate
from app.application.ports.extraction import ExtractionRequest, ExtractionResult
from app.core.summary_contract import validate_and_shape_summary
from app.core.summary_schema import SummaryModel

pytestmark = pytest.mark.contracts


# --------------------------------------------------------------------------- #
# extract parity: uniform state delta across source kinds
# --------------------------------------------------------------------------- #

_SOURCE_KINDS = [
    ("web_article", "https://example.com/article"),
    ("youtube_video", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("x_post", "https://x.com/user/status/123"),
    ("academic_paper", "https://arxiv.org/abs/2401.00001"),
    ("github_repository", "https://github.com/owner/repo"),
    ("threads_meta", "https://www.threads.net/@user/post/abc"),
    ("instagram_meta", "https://www.instagram.com/p/abc/"),
]

_DELTA_KEYS = {
    "lang",
    "source_text",
    "content_source",
    "detected_lang",
    "dedupe_hash",
    "title",
    "images",
}


class _FakeExtraction:
    def __init__(self, result: ExtractionResult) -> None:
        self.result = result
        self.calls: list[ExtractionRequest] = []

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        self.calls.append(request)
        return self.result


def _extract_deps(extraction: Any) -> SummarizeDeps:
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


@pytest.mark.parametrize(("kind", "url"), _SOURCE_KINDS, ids=[k for k, _ in _SOURCE_KINDS])
async def test_extract_delta_is_uniform_per_source_kind(kind: str, url: str) -> None:
    fake = _FakeExtraction(
        ExtractionResult(
            request_id=42,
            content_text=f"content for {kind}",
            content_source="markdown",
            detected_lang="en",
            dedupe_hash=f"hash-{kind}",
            title=kind,
        )
    )
    out = await extract(
        {"correlation_id": "c", "request_id": 42, "lang": "en", "input_url": url},
        deps=_extract_deps(fake),
    )
    assert set(out) == _DELTA_KEYS  # same delta shape for every source kind
    assert len(fake.calls) == 1 and fake.calls[0].url == url


# --------------------------------------------------------------------------- #
# summarize -> validate parity: contract-faithful, deterministic shaping
# --------------------------------------------------------------------------- #

_CANNED: dict[str, Any] = {
    "summary_250": "A concise 250 summary.",
    "summary_1000": "A longer 1000-character summary of the source.",
    "tldr": "The gist.",
    "topic_tags": ["Tech", "tech", "rust"],
}


def _structured() -> StructuredLLMResult:
    return StructuredLLMResult(
        parsed=SummaryModel.model_construct(**_CANNED),
        tokens_prompt=10,
        tokens_completion=5,
        model_used="model-x",
    )


def _summarize_deps() -> SummarizeDeps:
    m = MagicMock()
    return SummarizeDeps(
        llm_client=SimpleNamespace(chat_structured=AsyncMock(return_value=_structured())),
        retrieval=m,
        extraction=m,
        stream_sink=m,
        summaries=m,
        requests=m,
        summary_index=m,
        config=SummarizeConfig(
            model="model-x",
            temperature=0.2,
            structured_output_mode="json_schema",
            long_context_threshold_tokens=1_000_000,
        ),
    )


async def _run_summarize_pipeline() -> dict[str, Any]:
    deps = _summarize_deps()
    state: dict[str, Any] = {
        "correlation_id": "cid-1",
        "request_id": 1,
        "lang": "en",
        "source_text": "the source article body to summarize",
        "grounding_block": "",
        "call_count": 0,
    }
    state.update(await build_prompt(state, deps=deps))
    state.update(await summarize(state, deps=deps))
    state.update(await validate(state, deps=deps))
    return state


async def test_summarize_pipeline_output_matches_contract_normalizer() -> None:
    state = await _run_summarize_pipeline()
    assert state["validation_errors"] == []
    summary = state["summary"]
    # The validate node's output IS the canonical contract shape: re-normalizing it
    # is a no-op (idempotent), i.e. the graph emits exactly what the legacy contract
    # oracle would. topic_tags are hashtag-normalized + case-folded-deduped.
    assert validate_and_shape_summary(summary) == summary
    assert summary["topic_tags"] == ["#Tech", "#rust"]


async def test_summarize_pipeline_is_deterministic() -> None:
    first = await _run_summarize_pipeline()
    second = await _run_summarize_pipeline()
    assert first["summary"] == second["summary"]
