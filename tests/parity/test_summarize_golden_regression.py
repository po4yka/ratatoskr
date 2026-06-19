"""Frozen-golden regression lock for the summarize graph's shaped output (audit #14).

The legacy oracle (``PureSummaryService``) was deleted at the T9 cutover, so the
deferred "live graph vs. legacy" comparison the other parity files promised can no
longer run. The idempotence assertions in ``test_summarize_graph_parity`` /
``test_summarize_dual_path_parity`` pin that the graph output is a *fixed point* of
``validate_and_shape_summary`` -- but a change to the contract normalizer ITSELF
would move both the graph output AND the oracle in lockstep and slip through.

This file closes that gap with a true regression lock: the EXACT shaped JSON the
graph emits per source_kind is frozen under ``goldens/<kind>.json`` and asserted
byte-for-byte. Any drift in the graph pipeline OR the contract normalizer changes
the bytes and fails here. The fixtures were generated from the same canned
``StructuredLLMResult`` payloads (``_CANNED_BY_KIND``) the dual-path file uses, so
they stay coupled to that single source of canned truth.

Regenerating after an INTENTIONAL contract change: run
``python tests/parity/test_summarize_golden_regression.py`` (the module's
``__main__`` rewrites every golden), eyeball the diff, and commit it deliberately.

CI-safe: no langgraph / no DB (drives the node functions directly).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapter_models.llm.llm_models import StructuredLLMResult
from app.application.graphs.summarize.deps import SummarizeConfig, SummarizeDeps
from app.application.graphs.summarize.nodes import build_prompt, summarize, validate
from app.core.summary_schema import SummaryModel
from tests.parity.test_summarize_dual_path_parity import _CANNED_BY_KIND

pytestmark = pytest.mark.contracts

_GOLDENS_DIR = Path(__file__).parent / "goldens"


def _summarize_deps(canned: dict[str, Any]) -> SummarizeDeps:
    m = MagicMock()
    return SummarizeDeps(
        llm_client=SimpleNamespace(
            chat_structured=AsyncMock(
                return_value=StructuredLLMResult(
                    parsed=SummaryModel.model_construct(**canned),
                    tokens_prompt=10,
                    tokens_completion=5,
                    model_used="model-x",
                )
            )
        ),
        retrieval=m,
        extraction=m,
        stream_sink=m,
        summaries=m,
        requests=m,
        summary_index=m,
        config=SummarizeConfig(
            model="model-x",
            llm_provider="openrouter",
            temperature=0.2,
            structured_output_mode="json_schema",
            long_context_threshold_tokens=1_000_000,
        ),
    )


async def _run_pipeline(kind: str) -> dict[str, Any]:
    """Drive build_prompt -> summarize -> validate for a canned per-kind payload.

    Mirrors ``test_summarize_dual_path_parity._run_summarize_pipeline`` exactly so
    the goldens cannot drift from the canned-truth that file already pins.
    """
    deps = _summarize_deps(_CANNED_BY_KIND[kind])
    state: dict[str, Any] = {
        "correlation_id": "cid-golden",
        "request_id": 1,
        "lang": "en",
        "source_text": f"source body for {kind}",
        "grounding_block": "",
        "call_count": 0,
    }
    state.update(await build_prompt(state, deps=deps))
    state.update(await summarize(state, deps=deps))
    state.update(await validate(state, deps=deps))
    return state["summary"]


def _load_golden(kind: str) -> dict[str, Any]:
    return json.loads((_GOLDENS_DIR / f"{kind}.json").read_text(encoding="utf-8"))


def test_every_source_kind_has_a_frozen_golden() -> None:
    """Guard: a new source_kind in _CANNED_BY_KIND must ship its golden too."""
    on_disk = {p.stem for p in _GOLDENS_DIR.glob("*.json")}
    assert on_disk == set(_CANNED_BY_KIND), (
        "goldens/ is out of sync with _CANNED_BY_KIND -- regenerate via "
        "`python tests/parity/test_summarize_golden_regression.py`"
    )


@pytest.mark.parametrize("kind", sorted(_CANNED_BY_KIND), ids=sorted(_CANNED_BY_KIND))
async def test_graph_output_matches_frozen_golden(kind: str) -> None:
    """The graph's shaped summary == the byte-frozen golden for this source_kind.

    JSON round-trip on the live output normalizes tuple/JSON-type representation so
    the comparison is against the on-disk JSON exactly. A drift in the pipeline or
    the contract normalizer changes the bytes and fails here.
    """
    live = json.loads(json.dumps(await _run_pipeline(kind)))
    assert live == _load_golden(kind)


async def test_goldens_are_deterministic() -> None:
    """Two identical runs of one kind yield byte-identical output (no run-to-run drift)."""
    first = json.loads(json.dumps(await _run_pipeline("web_article")))
    second = json.loads(json.dumps(await _run_pipeline("web_article")))
    assert first == second


def _regenerate_goldens() -> None:  # pragma: no cover - dev-only regeneration helper
    """Rewrite every golden from the current pipeline output. Run via ``__main__``."""
    import asyncio

    _GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
    for kind in sorted(_CANNED_BY_KIND):
        summary = asyncio.run(_run_pipeline(kind))
        path = _GOLDENS_DIR / f"{kind}.json"
        path.write_text(
            json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {path}")


if __name__ == "__main__":  # pragma: no cover
    _regenerate_goldens()
