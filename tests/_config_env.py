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
    # OpenRouter model selection (no code default)
    "OPENROUTER_MODEL": "deepseek/deepseek-v4-flash",
    "OPENROUTER_FALLBACK_MODELS": (
        "qwen/qwen3.6-flash,qwen/qwen3.6-plus-04-02,"
        "moonshotai/kimi-k2-0905,minimax/minimax-m2"
    ),
    "OPENROUTER_FLASH_MODEL": "qwen/qwen3.6-flash",
    "OPENROUTER_FLASH_FALLBACK_MODELS": "qwen/qwen3.6-plus-04-02",
    "OPENROUTER_LONG_CONTEXT_MODEL": "minimax/minimax-m2",
    # Attachment vision model selection (no code default)
    "ATTACHMENT_VISION_MODEL": "qwen/qwen3-vl-32b-instruct",
    "ATTACHMENT_VISION_FALLBACK_MODELS": "moonshotai/kimi-k2.5",
    # OpenRouter behavioral tunables (no code default)
    "OPENROUTER_TEMPERATURE": "0.2",
    "OPENROUTER_ENABLE_STATS": "false",
    "OPENROUTER_ENABLE_STRUCTURED_OUTPUTS": "true",
    "OPENROUTER_STRUCTURED_OUTPUT_MODE": "json_schema",
    "OPENROUTER_REQUIRE_PARAMETERS": "true",
    "OPENROUTER_AUTO_FALLBACK_STRUCTURED": "true",
    "OPENROUTER_MAX_RESPONSE_SIZE_MB": "10",
    "OPENROUTER_ENABLE_PROMPT_CACHING": "true",
    "OPENROUTER_PROMPT_CACHE_TTL": "ephemeral",
    "OPENROUTER_PROMPT_CACHE_TTL_ANTHROPIC": "1h",
    "OPENROUTER_CACHE_SYSTEM_PROMPT": "true",
    "OPENROUTER_CACHE_LARGE_CONTENT_THRESHOLD": "4096",
    "OPENROUTER_TRANSPORT_RETRY_MAX_ATTEMPTS": "3",
    "OPENROUTER_TRANSPORT_RETRY_MIN_WAIT_SEC": "0.5",
    "OPENROUTER_TRANSPORT_RETRY_MAX_WAIT_SEC": "5.0",
    # AttachmentConfig behavioral tunables (no code default)
    "ATTACHMENT_PROCESSING_ENABLED": "true",
    "ARTICLE_VISION_ENABLED": "true",
    "ARTICLE_VISION_MIN_IMAGES": "1",
    "VISION_ROUTING_ROLE_FILTER_ENABLED": "true",
    "ATTACHMENT_VIDEO_STORAGE_PATH": "/data/video-sources",
    "ATTACHMENT_VIDEO_MAX_DOWNLOAD_SIZE_MB": "100",
    "ATTACHMENT_VIDEO_TIMEOUT_SEC": "120",
    "ATTACHMENT_VIDEO_CLEANUP_AFTER_HOURS": "24",
    "ATTACHMENT_VIDEO_FRAME_SAMPLE_COUNT": "4",
    "ATTACHMENT_VIDEO_AUDIO_TRANSCRIPTION_ENABLED": "true",
    "ATTACHMENT_MAX_IMAGE_SIZE_MB": "10",
    "ATTACHMENT_MAX_PDF_SIZE_MB": "20",
    "ATTACHMENT_MAX_PDF_PAGES": "50",
    "ATTACHMENT_IMAGE_MAX_DIMENSION": "2048",
    "ATTACHMENT_STORAGE_PATH": "/data/attachments",
    "ATTACHMENT_CLEANUP_AFTER_HOURS": "24",
    "ATTACHMENT_MAX_VISION_PAGES": "8",
    "ATTACHMENT_PDF_MIN_IMAGE_DIMENSION": "100",
    "ATTACHMENT_PDF_MAX_EMBEDDED_IMAGES": "8",
    "ATTACHMENT_PDF_MAX_IMAGE_URIS": "12",
    "ATTACHMENT_PDF_VECTOR_DRAW_THRESHOLD": "30",
    "ATTACHMENT_DOCUMENT_PROCESSING_ENABLED": "true",
    "ATTACHMENT_MAX_DOCUMENT_SIZE_MB": "20",
    "ATTACHMENT_MAX_DOCUMENT_CHARS": "45000",
    # YouTubeConfig behavioral tunables (no code default)
    "YOUTUBE_DOWNLOAD_ENABLED": "true",
    "YOUTUBE_STORAGE_PATH": "/data/videos",
    "YOUTUBE_MAX_VIDEO_SIZE_MB": "500",
    "YOUTUBE_MAX_STORAGE_GB": "100",
    "YOUTUBE_AUTO_CLEANUP_ENABLED": "true",
    "YOUTUBE_CLEANUP_AFTER_DAYS": "30",
    "YOUTUBE_PREFERRED_QUALITY": "1080p",
    "YOUTUBE_SUBTITLE_LANGUAGES": "en,ru",
}
