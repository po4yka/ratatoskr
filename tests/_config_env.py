"""Shared model-selection env baseline for config-building tests.

Model selection has no code default: production sources ``model``,
``fallback_models``, ``flash_model``, ``flash_fallback_models``,
``long_context_model`` (OpenRouter) and ``vision_model`` /
``vision_fallback_models`` (attachment) from ``config/ratatoskr.yaml``. Any test
that clears the environment (``patch.dict(os.environ, ..., clear=True)``) and
then builds ``Settings`` must supply these keys, or Pydantic hard-fails on the
now-required model fields. Spread this into such tests' env dicts.

The values mirror the documented defaults in ``config/ratatoskr.yaml.example``.
"""

from __future__ import annotations

MODEL_SELECTION_ENV: dict[str, str] = {
    "OPENROUTER_MODEL": "deepseek/deepseek-v4-flash",
    "OPENROUTER_FALLBACK_MODELS": (
        "qwen/qwen3.6-flash,qwen/qwen3.6-plus-04-02,"
        "moonshotai/kimi-k2-0905,minimax/minimax-m2"
    ),
    "OPENROUTER_FLASH_MODEL": "qwen/qwen3.6-flash",
    "OPENROUTER_FLASH_FALLBACK_MODELS": "qwen/qwen3.6-plus-04-02",
    "OPENROUTER_LONG_CONTEXT_MODEL": "minimax/minimax-m2",
    "ATTACHMENT_VISION_MODEL": "qwen/qwen3-vl-32b-instruct",
    "ATTACHMENT_VISION_FALLBACK_MODELS": "moonshotai/kimi-k2.5",
}
