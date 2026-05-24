"""Direct tests for ``SummaryRequestFactory._clamp_max_tokens_for_model``.

The clamp is the per-model max_tokens override applied at request build time.
Tests use a minimal stub runtime that exposes only ``cfg.openrouter``
because the clamp reads that one attribute. End-to-end factory wiring is
left to integration tests; this file pins the unit-level contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.adapters.content.summary_request_factory import SummaryRequestFactory


def _make_factory(overrides: dict[str, int]) -> SummaryRequestFactory:
    """Construct a factory with a stub runtime exposing the override dict."""
    runtime = SimpleNamespace(
        cfg=SimpleNamespace(openrouter=SimpleNamespace(per_model_max_tokens_overrides=overrides))
    )
    return SummaryRequestFactory(
        runtime=runtime,  # type: ignore[arg-type]
        select_max_tokens=lambda _content: 8192,
    )


class TestClampMaxTokensForModel:
    def test_no_override_returns_input_unchanged(self) -> None:
        factory = _make_factory({})
        result = factory._clamp_max_tokens_for_model(model_name="any/model", max_tokens=8192)
        assert result == 8192

    def test_no_override_with_none_input_returns_none(self) -> None:
        factory = _make_factory({})
        result = factory._clamp_max_tokens_for_model(model_name="any/model", max_tokens=None)
        assert result is None

    def test_override_clamps_down(self) -> None:
        factory = _make_factory({"qwen/qwen3-vl-32b-instruct": 3072})
        result = factory._clamp_max_tokens_for_model(
            model_name="qwen/qwen3-vl-32b-instruct", max_tokens=8192
        )
        assert result == 3072

    def test_override_does_not_raise_above_input(self) -> None:
        # If the caller asks for less than the override cap, the input wins.
        # We never RAISE max_tokens above what the call-site requested.
        factory = _make_factory({"qwen/qwen3-vl-32b-instruct": 8192})
        result = factory._clamp_max_tokens_for_model(
            model_name="qwen/qwen3-vl-32b-instruct", max_tokens=2048
        )
        assert result == 2048

    def test_override_with_none_input_returns_override(self) -> None:
        # When the call-site has no budget hint, the override becomes the cap.
        factory = _make_factory({"qwen/qwen3-vl-32b-instruct": 3072})
        result = factory._clamp_max_tokens_for_model(
            model_name="qwen/qwen3-vl-32b-instruct", max_tokens=None
        )
        assert result == 3072

    def test_override_only_matches_named_model(self) -> None:
        factory = _make_factory({"qwen/qwen3-vl-32b-instruct": 3072})
        result = factory._clamp_max_tokens_for_model(
            model_name="moonshotai/kimi-k2.5", max_tokens=8192
        )
        assert result == 8192

    def test_override_zero_is_ignored(self) -> None:
        # Defensive: a 0 override sneaks through dict-input config; treat as
        # absent rather than capping the model to zero tokens.
        factory = _make_factory({"qwen/qwen3-vl-32b-instruct": 0})
        result = factory._clamp_max_tokens_for_model(
            model_name="qwen/qwen3-vl-32b-instruct", max_tokens=8192
        )
        assert result == 8192

    def test_missing_overrides_attribute_is_safe(self) -> None:
        # Older config snapshots may lack the field; the clamp must fall back
        # to the input value rather than AttributeError-ing.
        runtime: Any = SimpleNamespace(cfg=SimpleNamespace(openrouter=SimpleNamespace()))
        factory = SummaryRequestFactory(
            runtime=runtime,
            select_max_tokens=lambda _content: None,
        )
        result = factory._clamp_max_tokens_for_model(model_name="any/model", max_tokens=4096)
        assert result == 4096
