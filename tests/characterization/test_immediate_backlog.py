from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.use_cases.get_unread_summaries import (
    GetUnreadSummariesQuery,
    GetUnreadSummariesUseCase,
)
from app.application.use_cases.mark_summary_as_read import (
    MarkSummaryAsReadCommand,
    MarkSummaryAsReadUseCase,
)
from app.application.use_cases.mark_summary_as_unread import (
    MarkSummaryAsUnreadCommand,
    MarkSummaryAsUnreadUseCase,
)
from tests.conftest import make_test_app_config
from tests.test_commands import BotSpy, FakeMessage


@pytest.mark.asyncio
async def test_characterization_summary_command_happy_path_preserved() -> None:
    """Lock current /summarize command behavior for a single URL."""
    pytest.importorskip("yt_dlp", reason="yt-dlp not installed")
    pytest.importorskip("youtube_transcript_api", reason="youtube-transcript-api not installed")

    from app.config import RuntimeConfig

    # The characterization is for the inline /summarize path. Production runs
    # with url_worker_enqueue_enabled=False (pinned in ratatoskr.yaml), but
    # the RuntimeConfig default is True; pin it explicitly so the bot dispatches
    # inline instead of handing the URL to the Taskiq worker.
    cfg = make_test_app_config(
        db_path=":memory:",
        allowed_user_ids=(1, 42),
        runtime=RuntimeConfig(
            db_path=":memory:",
            log_level="INFO",
            request_timeout_sec=5,
            preferred_lang="en",
            debug_payloads=False,
            url_worker_enqueue_enabled=False,
        ),
    )

    from app.adapters import telegram_bot as tbmod

    tbmod.Client = object
    tbmod.filters = None

    from unittest.mock import patch

    with (
        patch("app.adapters.openrouter.openrouter_client.OpenRouterClient") as mock_openrouter,
        patch(
            "app.infrastructure.persistence.repositories.user_repository.UserRepositoryAdapter.async_insert_user_interaction",
            new=AsyncMock(return_value=1),
        ),
    ):
        mock_openrouter.return_value = AsyncMock()
        bot = BotSpy(cfg=cfg, db=MagicMock())

        url = "https://example.com/characterization"
        msg = FakeMessage(f"/summarize {url}")

        await bot._on_message(msg)

    assert url in bot.seen_urls
    assert any(url in reply for reply in msg._replies)


@pytest.mark.asyncio
async def test_characterization_youtube_uses_vtt_fallback_when_api_transcript_empty(
    tmp_path,
) -> None:
    """Lock fallback behavior: empty API transcript should use downloaded VTT subtitles."""
    pytest.importorskip("yt_dlp", reason="yt-dlp not installed")
    pytest.importorskip("youtube_transcript_api", reason="youtube-transcript-api not installed")

    from app.adapters.content.platform_extraction.lifecycle import PlatformRequestLifecycle
    from app.adapters.youtube.platform_extractor import YouTubePlatformExtractor

    cfg = MagicMock()
    cfg.youtube.storage_path = str(tmp_path / "videos")
    cfg.youtube.max_video_size_mb = 500
    cfg.youtube.max_storage_gb = 100
    cfg.youtube.preferred_quality = "1080p"
    cfg.youtube.subtitle_languages = ["en"]
    cfg.youtube.auto_cleanup_enabled = False
    cfg.youtube.cleanup_after_days = 30

    rf = MagicMock()
    rf.send_message_draft = AsyncMock()
    rf.safe_reply = AsyncMock()
    rf.send_youtube_download_notification = AsyncMock()
    rf.send_youtube_download_complete_notification = AsyncMock()

    lifecycle = PlatformRequestLifecycle(
        response_formatter=rf,
        message_persistence=SimpleNamespace(
            request_repo=SimpleNamespace(
                async_create_request=AsyncMock(return_value=500),
                async_get_request_by_dedupe_hash=AsyncMock(return_value=None),
            ),
            user_repo=SimpleNamespace(
                async_upsert_chat=AsyncMock(),
                async_upsert_user=AsyncMock(),
            ),
            persist_message_snapshot=AsyncMock(),
        ),
        audit_func=lambda *_a, **_k: None,
        route_version=1,
    )
    downloader: Any = YouTubePlatformExtractor(
        cfg=cfg,
        db=MagicMock(),
        response_formatter=rf,
        audit_func=lambda *_a, **_k: None,
        lifecycle=lifecycle,
        request_repo=MagicMock(),
        video_repo=MagicMock(),
    )

    downloader._session_service.check_storage_limits = AsyncMock()
    downloader._session_service.request_repo = MagicMock()
    downloader._session_service.video_repo = MagicMock()

    downloader._session_service.request_repo.async_get_request_by_dedupe_hash = AsyncMock(
        return_value=None
    )
    downloader._session_service.video_repo.async_get_video_download_by_request = AsyncMock(
        return_value=None
    )
    downloader._session_service.video_repo.async_create_video_download = AsyncMock(return_value=900)
    downloader._session_service.video_repo.async_update_video_download_status = AsyncMock()
    downloader._session_service.video_repo.async_update_video_download = AsyncMock()
    downloader._session_service.request_repo.async_update_request_status = AsyncMock()
    downloader._session_service.request_repo.async_update_request_lang_detected = AsyncMock()
    downloader._session_service._create_video_request = AsyncMock(return_value=500)

    downloader._pipeline._extract_transcript_api = AsyncMock(return_value=("", "", False, "api"))
    downloader._pipeline._download_video_sync = MagicMock(
        return_value={
            "title": "Example video",
            "channel": "Channel",
            "channel_id": "ch-1",
            "duration": 123,
            "upload_date": "20260101",
            "view_count": 1000,
            "like_count": 100,
            "resolution": "1080p",
            "file_size": 1024 * 1024,
            "vcodec": "h264",
            "acodec": "aac",
            "format_id": "137+140",
            "subtitle_file_path": str(tmp_path / "captions.en.vtt"),
        }
    )
    downloader._pipeline._load_transcript_from_vtt = MagicMock(
        return_value=("vtt transcript body", "en")
    )

    result = await downloader.extract(
        SimpleNamespace(
            message=MagicMock(),
            url_text="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            normalized_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            correlation_id=None,
            interaction_id=None,
            silent=True,
            progress_tracker=None,
            request_id_override=None,
            mode="interactive",
        )
    )
    req_id = result.request_id
    combined_text = result.content_text
    transcript_source = result.content_source
    detected_lang = result.detected_lang
    _metadata = result.metadata

    assert req_id == 500
    assert transcript_source == "vtt"
    assert detected_lang == "en"
    assert "vtt transcript body" in combined_text
    downloader._session_service.video_repo.async_update_video_download.assert_awaited_once()


@pytest.mark.asyncio
async def test_characterization_unread_read_unread_transition_with_topic_filter() -> None:
    """Lock unread filtering and read-state transitions around topic-like filtering."""

    class InMemorySummaryRepo:
        def __init__(self) -> None:
            self.rows = {
                1: {
                    "id": 1,
                    "request_id": 10,
                    "lang": "en",
                    "json_payload": {
                        "title": "Rust migration notes",
                        "topic_tags": ["rust"],
                        "tldr": "r",
                        "summary_250": "rust",
                        "key_ideas": ["idea"],
                    },
                    "is_read": False,
                    "version": 1,
                },
                2: {
                    "id": 2,
                    "request_id": 11,
                    "lang": "en",
                    "json_payload": {
                        "title": "Python release notes",
                        "topic_tags": ["python"],
                        "tldr": "p",
                        "summary_250": "python",
                        "key_ideas": ["idea"],
                    },
                    "is_read": False,
                    "version": 1,
                },
            }

        async def async_get_unread_summaries(
            self, user_id=None, chat_id=None, limit=10, topic=None
        ):
            _ = (user_id, chat_id)
            unread = [v for v in self.rows.values() if not v["is_read"]]
            if topic:
                t = topic.casefold()
                unread = [r for r in unread if t in str(r["json_payload"]).casefold()]
            return unread[:limit]

        async def async_get_summary_by_id(self, summary_id: int):
            return self.rows.get(summary_id)

        async def async_mark_summary_as_read(self, summary_id: int):
            self.rows[summary_id]["is_read"] = True

        async def async_mark_summary_as_unread(self, summary_id: int):
            self.rows[summary_id]["is_read"] = False

        def to_domain_model(self, db_summary):
            from datetime import datetime

            from app.domain.models.summary import Summary

            return Summary(
                id=db_summary["id"],
                request_id=db_summary["request_id"],
                content=db_summary["json_payload"],
                language=db_summary["lang"],
                version=db_summary["version"],
                is_read=db_summary["is_read"],
                created_at=datetime.utcnow(),
            )

    repo: Any = InMemorySummaryRepo()
    unread_use_case = GetUnreadSummariesUseCase(repo)
    mark_read = MarkSummaryAsReadUseCase(repo)
    mark_unread = MarkSummaryAsUnreadUseCase(repo)

    rust_only = await unread_use_case.execute(
        GetUnreadSummariesQuery(user_id=1, chat_id=1, topic="rust")
    )
    assert [s.id for s in rust_only] == [1]

    await mark_read.execute(MarkSummaryAsReadCommand(summary_id=1, user_id=1))
    rust_after_read = await unread_use_case.execute(
        GetUnreadSummariesQuery(user_id=1, chat_id=1, topic="rust")
    )
    assert rust_after_read == []

    await mark_unread.execute(MarkSummaryAsUnreadCommand(summary_id=1, user_id=1))
    rust_after_unread = await unread_use_case.execute(
        GetUnreadSummariesQuery(user_id=1, chat_id=1, topic="rust")
    )
    assert [s.id for s in rust_after_unread] == [1]
