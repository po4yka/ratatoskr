"""Unit tests for the OPENROUTER_PER_MODEL_MAX_TOKENS_OVERRIDES validator.

Mirrors ``tests/config/test_per_model_timeout_overrides.py`` since both
validators share the ``model=value,model=value`` parse contract. Bad entries
must be logged and skipped, never raise.
"""

from __future__ import annotations

from app.config.llm import OpenRouterConfig

_API_KEY = "or_" + "z" * 20


def _make_openrouter(**kwargs: object) -> OpenRouterConfig:
    """Build an OpenRouterConfig with the minimum required api_key plus overrides."""
    return OpenRouterConfig.model_validate({"OPENROUTER_API_KEY": _API_KEY, **kwargs})


class TestPerModelMaxTokensOverridesParser:
    def test_empty_string_returns_empty_dict(self) -> None:
        cfg = _make_openrouter(OPENROUTER_PER_MODEL_MAX_TOKENS_OVERRIDES="")
        assert cfg.per_model_max_tokens_overrides == {}

    def test_none_returns_empty_dict(self) -> None:
        cfg = _make_openrouter(OPENROUTER_PER_MODEL_MAX_TOKENS_OVERRIDES=None)
        assert cfg.per_model_max_tokens_overrides == {}

    def test_default_is_empty_dict(self) -> None:
        cfg = OpenRouterConfig.model_validate({"OPENROUTER_API_KEY": _API_KEY})
        assert cfg.per_model_max_tokens_overrides == {}

    def test_single_entry(self) -> None:
        cfg = _make_openrouter(
            OPENROUTER_PER_MODEL_MAX_TOKENS_OVERRIDES="qwen/qwen3-vl-32b-instruct=3072"
        )
        assert cfg.per_model_max_tokens_overrides == {"qwen/qwen3-vl-32b-instruct": 3072}

    def test_multiple_entries(self) -> None:
        cfg = _make_openrouter(
            OPENROUTER_PER_MODEL_MAX_TOKENS_OVERRIDES=(
                "qwen/qwen3-vl-32b-instruct=3072,moonshotai/kimi-k2.5=8192"
            )
        )
        assert cfg.per_model_max_tokens_overrides == {
            "qwen/qwen3-vl-32b-instruct": 3072,
            "moonshotai/kimi-k2.5": 8192,
        }

    def test_whitespace_around_entries(self) -> None:
        cfg = _make_openrouter(
            OPENROUTER_PER_MODEL_MAX_TOKENS_OVERRIDES=(
                " qwen/qwen3-vl-32b-instruct = 3072 , moonshotai/kimi-k2.5 = 8192 "
            )
        )
        assert cfg.per_model_max_tokens_overrides == {
            "qwen/qwen3-vl-32b-instruct": 3072,
            "moonshotai/kimi-k2.5": 8192,
        }

    def test_malformed_entry_skipped_valid_kept(self) -> None:
        cfg = _make_openrouter(
            OPENROUTER_PER_MODEL_MAX_TOKENS_OVERRIDES=(
                "qwen/qwen3-vl-32b-instruct=3072,bad-no-equals,kimi=2048"
            )
        )
        assert cfg.per_model_max_tokens_overrides == {
            "qwen/qwen3-vl-32b-instruct": 3072,
            "kimi": 2048,
        }

    def test_non_integer_tokens_skipped(self) -> None:
        cfg = _make_openrouter(
            OPENROUTER_PER_MODEL_MAX_TOKENS_OVERRIDES="qwen/qwen3-vl=abc,kimi=2048"
        )
        assert cfg.per_model_max_tokens_overrides == {"kimi": 2048}

    def test_non_positive_tokens_skipped(self) -> None:
        cfg = _make_openrouter(
            OPENROUTER_PER_MODEL_MAX_TOKENS_OVERRIDES="qwen/qwen3-vl=0,kimi=-1,minimax=512"
        )
        assert cfg.per_model_max_tokens_overrides == {"minimax": 512}

    def test_empty_value_skipped(self) -> None:
        cfg = _make_openrouter(OPENROUTER_PER_MODEL_MAX_TOKENS_OVERRIDES="qwen/qwen3-vl=,kimi=2048")
        assert cfg.per_model_max_tokens_overrides == {"kimi": 2048}

    def test_dict_input_passthrough(self) -> None:
        cfg = _make_openrouter(
            OPENROUTER_PER_MODEL_MAX_TOKENS_OVERRIDES={
                "qwen/qwen3-vl-32b-instruct": 3072,
                "moonshotai/kimi-k2.5": 8192,
            }
        )
        assert cfg.per_model_max_tokens_overrides == {
            "qwen/qwen3-vl-32b-instruct": 3072,
            "moonshotai/kimi-k2.5": 8192,
        }

    def test_dict_input_bad_value_skipped(self) -> None:
        cfg = _make_openrouter(
            OPENROUTER_PER_MODEL_MAX_TOKENS_OVERRIDES={
                "qwen/qwen3-vl": "not-an-int",
                "kimi": 0,
                "minimax": 512,
            }
        )
        assert cfg.per_model_max_tokens_overrides == {"minimax": 512}
