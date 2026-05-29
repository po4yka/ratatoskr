"""Source classification helpers for mixed-source aggregation."""

from __future__ import annotations

from typing import Any

from app.adapters.academic.url_patterns import is_academic_paper_url
from app.adapters.telegram_source_helpers import (
    build_source_item_from_telegram_payload,
    classify_telegram_messages_source_kind,
)
from app.application.dto.aggregation import SourceSubmission, SourceSubmissionKind
from app.core.url_utils import (
    is_instagram_post_url,
    is_instagram_reel_url,
    is_threads_url,
    normalize_url,
)
from app.core.urls.twitter import is_twitter_article_url, is_twitter_url
from app.core.urls.youtube import is_youtube_url
from app.domain.models.source import SourceItem, SourceKind


def classify_url_source_kind(url: str, *, hint: str | None = None) -> SourceKind:
    """Classify a URL into the closest supported source kind."""

    if hint:
        try:
            return SourceKind(hint)
        except ValueError:
            pass

    normalized_url = normalize_url(url)
    if is_youtube_url(normalized_url):
        return SourceKind.YOUTUBE_VIDEO
    if is_twitter_article_url(normalized_url):
        return SourceKind.X_ARTICLE
    if is_twitter_url(normalized_url):
        return SourceKind.X_POST

    if is_threads_url(normalized_url):
        return SourceKind.THREADS_POST
    if is_instagram_reel_url(normalized_url):
        return SourceKind.INSTAGRAM_REEL
    if is_instagram_post_url(normalized_url):
        return SourceKind.INSTAGRAM_POST

    if is_academic_paper_url(normalized_url):
        return SourceKind.ACADEMIC_PAPER

    return SourceKind.WEB_ARTICLE


def classify_telegram_message_source_kind(message: Any) -> SourceKind:
    """Classify a Telegram-native submission into the closest source kind."""
    return classify_telegram_messages_source_kind(message)


def build_source_item_from_submission(submission: SourceSubmission) -> SourceItem:
    """Build a classified source item from a raw source submission."""

    metadata = dict(submission.metadata)
    if submission.submission_kind == SourceSubmissionKind.URL:
        url = submission.url or ""
        source_kind = classify_url_source_kind(
            url,
            hint=str(metadata.get("source_kind_hint") or "").strip() or None,
        )
        return SourceItem.create(
            kind=source_kind,
            original_value=url,
            metadata=metadata,
        )

    if submission.submission_kind == SourceSubmissionKind.TELEGRAM_MESSAGE:
        return build_source_item_from_telegram_payload(
            submission.telegram_message,
            metadata=metadata,
        )

    return SourceItem.create(kind=SourceKind.UNKNOWN, original_value="")


__all__ = [
    "build_source_item_from_submission",
    "classify_telegram_message_source_kind",
    "classify_url_source_kind",
]
