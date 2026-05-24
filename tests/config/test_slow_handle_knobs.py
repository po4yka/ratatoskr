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


class TestArticleVisionMinImages:
    def test_default_is_one(self) -> None:
        cfg = AttachmentConfig.model_validate({})
        assert cfg.article_vision_min_images == 1

    def test_env_override_raises_threshold(self) -> None:
        cfg = AttachmentConfig.model_validate({"ARTICLE_VISION_MIN_IMAGES": "3"})
        assert cfg.article_vision_min_images == 3

    def test_int_input_accepted(self) -> None:
        cfg = AttachmentConfig.model_validate({"ARTICLE_VISION_MIN_IMAGES": 5})
        assert cfg.article_vision_min_images == 5

    def test_zero_rejected(self) -> None:
        # A threshold of 0 would be meaningless (vision triggers on empty
        # image lists). The validator must reject it.
        with pytest.raises(ValidationError):
            AttachmentConfig.model_validate({"ARTICLE_VISION_MIN_IMAGES": "0"})

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AttachmentConfig.model_validate({"ARTICLE_VISION_MIN_IMAGES": "-1"})
