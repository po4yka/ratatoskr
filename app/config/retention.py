"""Retention policy configuration for raw artifact purge."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class RetentionConfig(BaseModel):
    """Per-subsystem TTL-based raw-data retention policy.

    A TTL of 0 means "never purge" for that subsystem.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=True,
        validation_alias="RETENTION_ENABLED",
        description="Master switch; when False no purge runs.",
    )
    cron: str = Field(
        default="0 3 * * *",
        validation_alias="RETENTION_CRON",
        description="UTC cron expression for the daily purge job.",
    )
    batch_size: int = Field(
        default=500,
        validation_alias="RETENTION_BATCH_SIZE",
        description="Max rows updated per subsystem per run.",
    )
    privacy_no_retention_mode: bool = Field(
        default=False,
        validation_alias="RETENTION_PRIVACY_NO_RETENTION_MODE",
        description="When true, skip avoidable raw payload persistence and purge raw fields on the next run.",
    )
    telegram_raw_days: int = Field(
        default=30,
        validation_alias="RETENTION_TELEGRAM_RAW_DAYS",
        description="Days to keep telegram_messages raw columns. 0 = never purge.",
    )
    crawl_content_days: int = Field(
        default=7,
        validation_alias=AliasChoices(
            "RETENTION_RAW_EXTRACTED_CONTENT_DAYS", "RETENTION_CRAWL_CONTENT_DAYS"
        ),
        description="Days to keep extracted raw content columns. 0 = never purge.",
    )
    llm_payload_days: int = Field(
        default=90,
        validation_alias=AliasChoices(
            "RETENTION_LLM_PROMPT_RESPONSE_DAYS", "RETENTION_LLM_PAYLOAD_DAYS"
        ),
        description="Days to keep llm_calls request/response columns. 0 = never purge.",
    )
    llm_prompt_response_policy: Literal["full", "metadata_only"] = Field(
        default="full",
        validation_alias="RETENTION_LLM_PROMPT_RESPONSE_POLICY",
        description="Whether to persist full LLM prompt/response payloads or only metadata.",
    )
    video_transcript_days: int = Field(
        default=30,
        validation_alias="RETENTION_VIDEO_TRANSCRIPT_DAYS",
        description="Days to keep video_downloads.transcript_text. 0 = never purge.",
    )
    downloaded_media_days: int = Field(
        default=30,
        validation_alias="RETENTION_DOWNLOADED_MEDIA_DAYS",
        description="Days to keep downloaded media/subtitle/metadata files. 0 = never purge.",
    )
    export_temp_file_hours: int = Field(
        default=24,
        validation_alias="RETENTION_EXPORT_TEMP_FILE_HOURS",
        description="Hours to keep orphaned export temp files. 0 = never purge.",
    )
    interaction_text_days: int = Field(
        default=30,
        validation_alias="RETENTION_INTERACTION_TEXT_DAYS",
        description="Days to keep user_interactions.input_text. 0 = never purge.",
    )
    request_content_days: int = Field(
        default=30,
        validation_alias="RETENTION_REQUEST_CONTENT_DAYS",
        description="Days to keep requests.content_text + error_context_json. 0 = never purge.",
    )

    @field_validator("cron", mode="before")
    @classmethod
    def _validate_cron(cls, value: Any) -> str:
        if value in (None, ""):
            return "0 3 * * *"
        cron = str(value).strip()
        if len(cron.split()) != 5:
            msg = "Retention cron must be a valid 5-field cron expression"
            raise ValueError(msg)
        return cron

    @field_validator("batch_size", mode="before")
    @classmethod
    def _validate_batch_size(cls, value: Any) -> int:
        parsed = int(str(value)) if value not in (None, "") else 500
        if parsed < 1 or parsed > 10_000:
            msg = "Retention batch_size must be between 1 and 10000"
            raise ValueError(msg)
        return parsed

    @field_validator(
        "telegram_raw_days",
        "crawl_content_days",
        "llm_payload_days",
        "video_transcript_days",
        "downloaded_media_days",
        "interaction_text_days",
        "request_content_days",
        mode="before",
    )
    @classmethod
    def _validate_ttl_days(cls, value: Any, info: ValidationInfo) -> int:
        default = cls.model_fields[info.field_name].default
        parsed = int(str(value)) if value not in (None, "") else default
        if parsed < 0:
            msg = f"{info.field_name} must be >= 0 (0 means 'never purge')"
            raise ValueError(msg)
        return parsed

    @field_validator("export_temp_file_hours", mode="before")
    @classmethod
    def _validate_ttl_hours(cls, value: Any, info: ValidationInfo) -> int:
        default = cls.model_fields[info.field_name].default
        parsed = int(str(value)) if value not in (None, "") else default
        if parsed < 0:
            msg = f"{info.field_name} must be >= 0 (0 means 'never purge')"
            raise ValueError(msg)
        return parsed

    @field_validator("llm_prompt_response_policy", mode="before")
    @classmethod
    def _validate_llm_prompt_response_policy(cls, value: Any) -> str:
        if value in (None, ""):
            return "full"
        parsed = str(value).strip().lower().replace("-", "_")
        if parsed not in {"full", "metadata_only"}:
            msg = "llm_prompt_response_policy must be one of: full, metadata_only"
            raise ValueError(msg)
        return parsed

    @property
    def persist_raw_extracted_content(self) -> bool:
        """Whether new crawl-result rows should store raw markdown/html payloads."""
        return not self.privacy_no_retention_mode

    @property
    def persist_llm_prompt_response_payloads(self) -> bool:
        """Whether new LLM-call rows should store request/response payload columns."""
        return not self.privacy_no_retention_mode and self.llm_prompt_response_policy == "full"

    @property
    def export_temp_file_max_age_seconds(self) -> int:
        """Export temp-file TTL in seconds; 0 means never purge."""
        return self.export_temp_file_hours * 60 * 60
