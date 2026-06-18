"""Unit tests for the LLM cascade timeout-floor policy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from app.config import AppConfig
from app.application.services.llm_cascade_timeout import compute_llm_cascade_floor


def _cfg(per_model_min: float, fallback_models: object) -> AppConfig:
    return cast(
        "AppConfig",
        SimpleNamespace(
            runtime=SimpleNamespace(llm_per_model_timeout_min_sec=per_model_min),
            openrouter=SimpleNamespace(fallback_models=fallback_models),
        ),
    )


def test_floor_single_model_adds_scraping_overhead() -> None:
    # 1 model * 120s + 60s overhead
    assert compute_llm_cascade_floor(_cfg(120.0, [])) == 180.0


def test_floor_scales_with_fallback_models() -> None:
    # (1 primary + 2 fallbacks) * 100s + 60s
    assert compute_llm_cascade_floor(_cfg(100.0, ["m1", "m2"])) == 360.0


def test_floor_uses_default_per_model_min_when_unset() -> None:
    cfg = cast(
        "AppConfig", SimpleNamespace(runtime=SimpleNamespace(), openrouter=SimpleNamespace())
    )
    # default 120s per model, 1 model, + 60s
    assert compute_llm_cascade_floor(cfg) == 180.0


def test_floor_treats_none_fallbacks_as_empty() -> None:
    assert compute_llm_cascade_floor(_cfg(120.0, None)) == 180.0
