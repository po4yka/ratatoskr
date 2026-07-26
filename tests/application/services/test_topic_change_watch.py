from datetime import UTC, datetime

from app.application.services.topic_change_watch import build_topic_change_brief


def test_topic_change_brief_cites_new_scored_signals_and_preserves_provenance() -> None:
    brief, provenance = build_topic_change_brief(
        topic_name="AI",
        since=datetime(2026, 7, 10, 12, tzinfo=UTC),
        signals=[
            {
                "signal_id": 7,
                "feed_item_id": 17,
                "source_id": 3,
                "title": "New model release",
                "url": "https://example.com/release",
                "final_score": 0.91,
            }
        ],
    )

    assert "[signal:7]" in brief
    assert "Changes since 2026-07-10 12:00 UTC" in brief
    assert provenance == [
        {
            "signal_id": 7,
            "feed_item_id": 17,
            "source_id": 3,
            "url": "https://example.com/release",
            "final_score": 0.91,
        }
    ]
