"""Field-validator tests for the slow-handle mitigation knobs.

Covers:
- ``RuntimeConfig.summarization_max_retries`` (``SUMMARIZATION_MAX_RETRIES``)
- ``AttachmentConfig.article_vision_min_images`` (``ARTICLE_VISION_MIN_IMAGES``)

Both fields gate the dominant cost driver for image-bearing HTML articles
(vision-routing x multi-attempt cascade). Out-of-range values must raise so
operators see a misconfiguration at startup rather than silently inheriting
the default.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.media import AttachmentConfig
from app.config.runtime import RuntimeConfig
from tests._config_env import MODEL_SELECTION_ENV


class TestSummarizationMaxRetries:
    def test_default_is_three(self) -> None:
        cfg = RuntimeConfig.model_validate({})
        assert cfg.summarization_max_retries == 3

    def test_env_override_lowers(self) -> None:
        cfg = RuntimeConfig.model_validate({"SUMMARIZATION_MAX_RETRIES": "1"})
        assert cfg.summarization_max_retries == 1

    def test_env_override_raises(self) -> None:
        cfg = RuntimeConfig.model_validate({"SUMMARIZATION_MAX_RETRIES": "5"})
        assert cfg.summarization_max_retries == 5

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({"SUMMARIZATION_MAX_RETRIES": "0"})

    def test_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({"SUMMARIZATION_MAX_RETRIES": "11"})


class TestLlmRequestSlowThresholdSec:
    def test_default_is_300(self) -> None:
        cfg = RuntimeConfig.model_validate({})
        assert cfg.llm_request_slow_threshold_sec == 300.0

    def test_env_override_accepted(self) -> None:
        cfg = RuntimeConfig.model_validate({"LLM_REQUEST_SLOW_THRESHOLD_SEC": "60"})
        assert cfg.llm_request_slow_threshold_sec == 60.0

    def test_below_minimum_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({"LLM_REQUEST_SLOW_THRESHOLD_SEC": "0.5"})


class TestLlmBudgetTightRatio:
    def test_default_is_0_6(self) -> None:
        cfg = RuntimeConfig.model_validate({})
        assert cfg.llm_budget_tight_ratio == 0.6

    def test_env_override_accepted(self) -> None:
        cfg = RuntimeConfig.model_validate({"LLM_BUDGET_TIGHT_RATIO": "0.8"})
        assert cfg.llm_budget_tight_ratio == 0.8

    def test_lower_value_accepted(self) -> None:
        cfg = RuntimeConfig.model_validate({"LLM_BUDGET_TIGHT_RATIO": "0.3"})
        assert cfg.llm_budget_tight_ratio == 0.3

    def test_one_accepted(self) -> None:
        cfg = RuntimeConfig.model_validate({"LLM_BUDGET_TIGHT_RATIO": "1.0"})
        assert cfg.llm_budget_tight_ratio == 1.0

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({"LLM_BUDGET_TIGHT_RATIO": "0.0"})

    def test_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({"LLM_BUDGET_TIGHT_RATIO": "1.1"})

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({"LLM_BUDGET_TIGHT_RATIO": "-0.5"})


class TestLlmTruncationMaxCount:
    def test_default_is_two(self) -> None:
        cfg = RuntimeConfig.model_validate({})
        assert cfg.llm_truncation_max_count == 2

    def test_env_override_accepted(self) -> None:
        cfg = RuntimeConfig.model_validate({"LLM_TRUNCATION_MAX_COUNT": "3"})
        assert cfg.llm_truncation_max_count == 3

    def test_one_accepted(self) -> None:
        cfg = RuntimeConfig.model_validate({"LLM_TRUNCATION_MAX_COUNT": "1"})
        assert cfg.llm_truncation_max_count == 1

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({"LLM_TRUNCATION_MAX_COUNT": "0"})

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({"LLM_TRUNCATION_MAX_COUNT": "-1"})


class TestUrlFlowLeaseTtlSec:
    def test_default_is_900(self) -> None:
        cfg = RuntimeConfig.model_validate({})
        assert cfg.url_flow_lease_ttl_sec == 900

    def test_env_override_accepted(self) -> None:
        cfg = RuntimeConfig.model_validate({"URL_FLOW_LEASE_TTL_SEC": "1800"})
        assert cfg.url_flow_lease_ttl_sec == 1800

    def test_below_minimum_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({"URL_FLOW_LEASE_TTL_SEC": "30"})

    def test_above_maximum_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({"URL_FLOW_LEASE_TTL_SEC": "7200"})

    def test_minimum_boundary_accepted(self) -> None:
        cfg = RuntimeConfig.model_validate({"URL_FLOW_LEASE_TTL_SEC": "60"})
        assert cfg.url_flow_lease_ttl_sec == 60

    def test_maximum_boundary_accepted(self) -> None:
        cfg = RuntimeConfig.model_validate({"URL_FLOW_LEASE_TTL_SEC": "3600"})
        assert cfg.url_flow_lease_ttl_sec == 3600


class TestArticleVisionMinImages:
    def test_default_is_one(self) -> None:
        cfg = AttachmentConfig.model_validate({**MODEL_SELECTION_ENV,})
        assert cfg.article_vision_min_images == 1

    def test_env_override_raises_threshold(self) -> None:
        cfg = AttachmentConfig.model_validate({**MODEL_SELECTION_ENV,"ARTICLE_VISION_MIN_IMAGES": "3"})
        assert cfg.article_vision_min_images == 3

    def test_int_input_accepted(self) -> None:
        cfg = AttachmentConfig.model_validate({**MODEL_SELECTION_ENV,"ARTICLE_VISION_MIN_IMAGES": 5})
        assert cfg.article_vision_min_images == 5

    def test_zero_rejected(self) -> None:
        # A threshold of 0 would be meaningless (vision triggers on empty
        # image lists). The validator must reject it.
        with pytest.raises(ValidationError):
            AttachmentConfig.model_validate({**MODEL_SELECTION_ENV,"ARTICLE_VISION_MIN_IMAGES": "0"})

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AttachmentConfig.model_validate({**MODEL_SELECTION_ENV,"ARTICLE_VISION_MIN_IMAGES": "-1"})


class TestLlmStickyFailureForceFallback:
    def test_default_is_true(self) -> None:
        cfg = RuntimeConfig.model_validate({})
        assert cfg.llm_sticky_failure_force_fallback is True

    def test_env_override_false(self) -> None:
        cfg = RuntimeConfig.model_validate({"LLM_STICKY_FAILURE_FORCE_FALLBACK": "false"})
        assert cfg.llm_sticky_failure_force_fallback is False

    def test_env_override_true_explicit(self) -> None:
        cfg = RuntimeConfig.model_validate({"LLM_STICKY_FAILURE_FORCE_FALLBACK": "true"})
        assert cfg.llm_sticky_failure_force_fallback is True
