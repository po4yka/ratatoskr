"""Policy: the minimum outer per-URL timeout that covers the full LLM cascade.

Extracted from the Telegram DI wiring so the timeout-budget policy lives in the
application layer (and is unit-testable) rather than in composition code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.config import AppConfig

logger = get_logger(__name__)

# Headroom added on top of the cascade for scraping and non-LLM handler work.
_SCRAPING_OVERHEAD_SEC = 60.0
# Fallback per-model floor when cfg.runtime.llm_per_model_timeout_min_sec is unset.
_DEFAULT_PER_MODEL_MIN_SEC = 120.0


def compute_llm_cascade_floor(cfg: AppConfig) -> float:
    """Compute the minimum outer per-URL timeout that covers the full LLM cascade.

    The inner LLM cascade in ``_invoke_llm`` can run for up to
    ``num_models * per_model_min`` seconds when every model in the fallback chain
    hits its per-model floor timeout. If the adaptive estimate is smaller than
    this value the outer asyncio timeout fires prematurely, producing "Timed out
    after Xs" even though a fallback model might have succeeded within the full
    cascade window. An additional ``_SCRAPING_OVERHEAD_SEC`` is added for scraping
    and non-LLM handler overhead.
    """
    per_model_min = float(
        getattr(cfg.runtime, "llm_per_model_timeout_min_sec", _DEFAULT_PER_MODEL_MIN_SEC)
    )
    num_models = 1 + len(getattr(cfg.openrouter, "fallback_models", ()) or ())
    floor = num_models * per_model_min + _SCRAPING_OVERHEAD_SEC
    logger.debug(
        "llm_cascade_floor_computed",
        extra={
            "num_models": num_models,
            "per_model_min_sec": per_model_min,
            "scraping_overhead_sec": _SCRAPING_OVERHEAD_SEC,
            "floor_sec": floor,
        },
    )
    return floor
