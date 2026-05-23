"""Unit tests for mixed-source aggregation domain models."""

from __future__ import annotations

from app.domain.models.source import AggregationRequest, SourceBundle, SourceItem, SourceKind


def test_source_item_prefers_external_id_for_stable_identity() -> None:
    first = SourceItem.create(
        kind=SourceKind.X_POST,
        original_value="https://x.com/user/status/123?s=20&t=aaa",
        external_id="123",
    )
    second = SourceItem.create(
        kind=SourceKind.X_POST,
        original_value="https://twitter.com/user/status/123",
        external_id="123",
    )

    assert first.stable_id == second.stable_id
    assert first.dedupe_key == "x_post:external:123"


def test_source_item_uses_telegram_message_locator() -> None:
    item = SourceItem.create(
        kind=SourceKind.TELEGRAM_POST_WITH_IMAGES,
        telegram_chat_id=-100123456,
        telegram_message_id=77,
        original_value="Forwarded post",
    )

    assert item.dedupe_key == "telegram_post_with_images:telegram_message:-100123456:77"
    assert item.telegram_message_id == 77


def test_source_bundle_reports_duplicate_positions_and_unique_items() -> None:
    first = SourceItem.create(
        kind=SourceKind.WEB_ARTICLE,
        original_value="https://example.com/post?utm_source=test",
    )
    second = SourceItem.create(
        kind=SourceKind.YOUTUBE_VIDEO,
        original_value="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        external_id="dQw4w9WgXcQ",
    )
    duplicate = SourceItem.create(
        kind=SourceKind.WEB_ARTICLE,
        original_value="https://example.com/post",
    )

    bundle = SourceBundle.from_items([first, second, duplicate])

    assert bundle.duplicate_positions() == {2: 0}
    assert bundle.unique_items == (first, second)


def test_source_kind_includes_fieldtheory_bookmark() -> None:
    assert SourceKind.FIELDTHEORY_BOOKMARK.value == "fieldtheory_bookmark"
    assert SourceKind("fieldtheory_bookmark") is SourceKind.FIELDTHEORY_BOOKMARK


def test_aggregation_request_exposes_total_items() -> None:
    request = AggregationRequest.from_items(
        [
            SourceItem.create(
                kind=SourceKind.WEB_ARTICLE,
                original_value="https://example.com/a",
            ),
            SourceItem.create(
                kind=SourceKind.WEB_ARTICLE,
                original_value="https://example.com/b",
            ),
        ],
        correlation_id="agg-1",
        user_id=42,
    )

    assert request.total_items == 2
    assert request.correlation_id == "agg-1"
    assert request.user_id == 42
