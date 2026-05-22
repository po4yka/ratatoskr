"""Tests for retention policy configuration."""

from __future__ import annotations

import pytest

from app.config.retention import RetentionConfig


def test_retention_config_accepts_privacy_controls() -> None:
    cfg = RetentionConfig(
        RETENTION_PRIVACY_NO_RETENTION_MODE="true",
        RETENTION_RAW_EXTRACTED_CONTENT_DAYS="2",
        RETENTION_LLM_PROMPT_RESPONSE_DAYS="3",
        RETENTION_LLM_PROMPT_RESPONSE_POLICY="metadata-only",
        RETENTION_DOWNLOADED_MEDIA_DAYS="4",
        RETENTION_EXPORT_TEMP_FILE_HOURS="5",
    )

    assert cfg.privacy_no_retention_mode is True
    assert cfg.crawl_content_days == 2
    assert cfg.llm_payload_days == 3
    assert cfg.llm_prompt_response_policy == "metadata_only"
    assert cfg.downloaded_media_days == 4
    assert cfg.export_temp_file_max_age_seconds == 18_000
    assert cfg.persist_raw_extracted_content is False
    assert cfg.persist_llm_prompt_response_payloads is False


def test_retention_config_rejects_unknown_llm_policy() -> None:
    with pytest.raises(ValueError, match="llm_prompt_response_policy"):
        RetentionConfig(RETENTION_LLM_PROMPT_RESPONSE_POLICY="raw")
