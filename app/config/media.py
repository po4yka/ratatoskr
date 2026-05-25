from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.core.logging_utils import get_logger

from ._validators import validate_model_name

LOGGER = get_logger(__name__)


class YouTubeConfig(BaseModel):
    """YouTube video download and storage configuration."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=True,
        validation_alias="YOUTUBE_DOWNLOAD_ENABLED",
        description="Enable YouTube video downloading",
    )

    storage_path: str = Field(
        default="/data/videos",
        validation_alias="YOUTUBE_STORAGE_PATH",
        description="Path to store downloaded videos",
    )

    max_video_size_mb: int = Field(
        default=500,
        validation_alias="YOUTUBE_MAX_VIDEO_SIZE_MB",
        description="Maximum video file size in MB",
    )

    max_storage_gb: int = Field(
        default=100,
        validation_alias="YOUTUBE_MAX_STORAGE_GB",
        description="Maximum total storage for videos in GB",
    )

    auto_cleanup_enabled: bool = Field(
        default=True,
        validation_alias="YOUTUBE_AUTO_CLEANUP_ENABLED",
        description="Enable automatic cleanup of old videos",
    )

    cleanup_after_days: int = Field(
        default=30,
        validation_alias="YOUTUBE_CLEANUP_AFTER_DAYS",
        description="Delete videos older than this many days",
    )

    preferred_quality: str = Field(
        default="1080p",
        validation_alias="YOUTUBE_PREFERRED_QUALITY",
        description="Preferred video quality (1080p, 720p, 480p)",
    )

    subtitle_languages: list[str] = Field(
        default=["en", "ru"],
        validation_alias="YOUTUBE_SUBTITLE_LANGUAGES",
        description="Preferred subtitle languages (fallback order)",
    )

    @field_validator("subtitle_languages", mode="before")
    @classmethod
    def _parse_subtitle_languages(cls, value: Any) -> list[str]:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [lang.strip() for lang in value.split(",") if lang.strip()]
        return ["en", "ru"]

    @field_validator("max_video_size_mb", "max_storage_gb", "cleanup_after_days", mode="before")
    @classmethod
    def _parse_int_fields(cls, value: Any, info: ValidationInfo) -> int:
        if value in (None, ""):
            default = cls.model_fields[info.field_name].default
            return int(default)
        try:
            return int(str(value))
        except ValueError as exc:
            msg = f"{info.field_name.replace('_', ' ')} must be a valid integer"
            raise ValueError(msg) from exc

    @field_validator("preferred_quality", mode="before")
    @classmethod
    def _validate_preferred_quality(cls, value: Any) -> str:
        if value in (None, ""):
            return "1080p"
        valid_qualities = {"1080p", "720p", "480p", "360p", "240p"}
        value_str = str(value).lower().strip()
        if value_str not in valid_qualities:
            msg = f"preferred_quality must be one of: {', '.join(sorted(valid_qualities))}"
            raise ValueError(msg)
        return value_str


class AttachmentConfig(BaseModel):
    """Attachment processing configuration for images and PDFs."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=True,
        validation_alias="ATTACHMENT_PROCESSING_ENABLED",
        description="Enable attachment processing (images, PDFs)",
    )

    article_vision_enabled: bool = Field(
        default=True,
        validation_alias="ARTICLE_VISION_ENABLED",
        description="Send extracted article images to a vision model for richer summaries",
    )

    article_vision_min_images: int = Field(
        default=1,
        ge=1,
        validation_alias="ARTICLE_VISION_MIN_IMAGES",
        description=(
            "Minimum number of extracted images required to route an HTML article to the "
            "vision model. Articles with fewer images use the text path. Defaults to 1 "
            "(any image triggers vision); raise to 2-3 to skip vision for articles that "
            "only have a header/OG image."
        ),
    )

    vision_routing_role_filter_enabled: bool = Field(
        default=True,
        validation_alias="VISION_ROUTING_ROLE_FILTER_ENABLED",
        description=(
            "Drop decorative header images (og:image/ogImage) and small thumbnails "
            "from the article image candidate list before the vision-routing count "
            "gate fires, when at least one content-area image survives. Articles "
            "with only an OG header thus take the text path instead of paying the "
            "vision model latency for a decorative thumbnail. Disable to restore "
            "the prior count-only routing."
        ),
    )

    vision_model: str = Field(
        default="qwen/qwen3-vl-32b-instruct",
        validation_alias="ATTACHMENT_VISION_MODEL",
        description="Vision-capable model for image and scanned PDF analysis",
    )

    vision_fallback_models: tuple[str, ...] = Field(
        default_factory=lambda: ("moonshotai/kimi-k2.5",),
        validation_alias="ATTACHMENT_VISION_FALLBACK_MODELS",
        description="Fallback vision models if primary fails",
    )

    video_storage_path: str = Field(
        default="/data/video-sources",
        validation_alias="ATTACHMENT_VIDEO_STORAGE_PATH",
        description="Path to store temporary non-YouTube video assets and frame samples",
    )

    video_max_download_size_mb: int = Field(
        default=100,
        validation_alias="ATTACHMENT_VIDEO_MAX_DOWNLOAD_SIZE_MB",
        description="Maximum download size for non-YouTube video assets in MB",
    )

    video_timeout_sec: int = Field(
        default=120,
        validation_alias="ATTACHMENT_VIDEO_TIMEOUT_SEC",
        description="Timeout budget for non-YouTube video extraction in seconds",
    )

    video_cleanup_after_hours: int = Field(
        default=24,
        validation_alias="ATTACHMENT_VIDEO_CLEANUP_AFTER_HOURS",
        description="Delete temporary non-YouTube video assets after this many hours",
    )

    video_frame_sample_count: int = Field(
        default=4,
        validation_alias="ATTACHMENT_VIDEO_FRAME_SAMPLE_COUNT",
        description="Maximum number of frame samples to analyze for OCR fallback",
    )

    video_audio_transcription_enabled: bool = Field(
        default=True,
        validation_alias="ATTACHMENT_VIDEO_AUDIO_TRANSCRIPTION_ENABLED",
        description="Allow audio-transcript fallback for supported non-YouTube video sources",
    )

    max_image_size_mb: int = Field(
        default=10,
        validation_alias="ATTACHMENT_MAX_IMAGE_SIZE_MB",
        description="Maximum image file size in MB",
    )

    max_pdf_size_mb: int = Field(
        default=20,
        validation_alias="ATTACHMENT_MAX_PDF_SIZE_MB",
        description="Maximum PDF file size in MB",
    )

    max_pdf_pages: int = Field(
        default=50,
        validation_alias="ATTACHMENT_MAX_PDF_PAGES",
        description="Maximum PDF pages to process",
    )

    image_max_dimension: int = Field(
        default=2048,
        validation_alias="ATTACHMENT_IMAGE_MAX_DIMENSION",
        description="Maximum image dimension (width or height) before resizing",
    )

    storage_path: str = Field(
        default="/data/attachments",
        validation_alias="ATTACHMENT_STORAGE_PATH",
        description="Temporary storage path for downloaded attachments",
    )

    cleanup_after_hours: int = Field(
        default=24,
        validation_alias="ATTACHMENT_CLEANUP_AFTER_HOURS",
        description="Delete attachment files after this many hours",
    )

    max_vision_pages_per_pdf: int = Field(
        default=8,
        validation_alias="ATTACHMENT_MAX_VISION_PAGES",
        description="Maximum number of sparse/scanned/figure PDF pages to render for vision LLM",
    )

    pdf_min_image_dimension: int = Field(
        default=100,
        validation_alias="ATTACHMENT_PDF_MIN_IMAGE_DIMENSION",
        description="Minimum image dimension (px) for embedded PDF images to be extracted",
    )

    pdf_max_embedded_images: int = Field(
        default=8,
        validation_alias="ATTACHMENT_PDF_MAX_EMBEDDED_IMAGES",
        description="Maximum number of embedded raster images to extract per PDF",
    )

    pdf_max_image_uris_total: int = Field(
        default=12,
        validation_alias="ATTACHMENT_PDF_MAX_IMAGE_URIS",
        description="Maximum total image URIs (rendered pages + embedded) sent to vision model",
    )

    pdf_vector_draw_threshold: int = Field(
        default=30,
        validation_alias="ATTACHMENT_PDF_VECTOR_DRAW_THRESHOLD",
        description="Minimum vector path count on a page to treat it as a figure page for vision rendering",
    )

    document_processing_enabled: bool = Field(
        default=True,
        validation_alias="ATTACHMENT_DOCUMENT_PROCESSING_ENABLED",
        description="Enable Office / EPUB / HTML / structured-text document processing via markitdown",
    )

    max_document_size_mb: int = Field(
        default=20,
        validation_alias="ATTACHMENT_MAX_DOCUMENT_SIZE_MB",
        description="Maximum size for non-PDF documents (docx/pptx/xlsx/epub/...) in MB",
    )

    max_document_chars: int = Field(
        default=45_000,
        validation_alias="ATTACHMENT_MAX_DOCUMENT_CHARS",
        description="Truncate extracted Markdown to this many characters before LLM summarization",
    )

    @field_validator(
        "max_image_size_mb",
        "max_pdf_size_mb",
        "max_pdf_pages",
        "image_max_dimension",
        "cleanup_after_hours",
        "max_vision_pages_per_pdf",
        "pdf_min_image_dimension",
        "pdf_max_embedded_images",
        "pdf_max_image_uris_total",
        "pdf_vector_draw_threshold",
        "video_max_download_size_mb",
        "video_timeout_sec",
        "video_cleanup_after_hours",
        "video_frame_sample_count",
        "max_document_size_mb",
        "max_document_chars",
        mode="before",
    )
    @classmethod
    def _parse_int_fields(cls, value: Any, info: ValidationInfo) -> int:
        if value in (None, ""):
            default = cls.model_fields[info.field_name].default
            return int(default)
        try:
            return int(str(value))
        except ValueError as exc:
            msg = f"{info.field_name.replace('_', ' ')} must be a valid integer"
            raise ValueError(msg) from exc

    @field_validator("vision_model", mode="before")
    @classmethod
    def _validate_vision_model(cls, value: Any) -> str:
        if value in (None, ""):
            return "qwen/qwen3-vl-32b-instruct"
        return validate_model_name(str(value))

    @field_validator("vision_fallback_models", mode="before")
    @classmethod
    def _parse_vision_fallback_models(cls, value: Any) -> tuple[str, ...]:
        if value in (None, ""):
            return ()
        iterable = value if isinstance(value, list | tuple) else str(value).split(",")

        validated: list[str] = []
        for raw in iterable:
            candidate = str(raw).strip()
            if not candidate:
                continue
            try:
                validated.append(validate_model_name(candidate))
            except ValueError as exc:
                LOGGER.debug("invalid_preferred_model_ignored", extra={"error": str(exc)})
                continue
        return tuple(validated)
