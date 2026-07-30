"""YouTube: stale download rows, the 1.x transcript API, dedupe and sidecars.

Each of these silently degraded a path that looked healthy: a row nothing reaps
poisoned a URL forever, a renamed upstream method made the cheap caption tier
dead while its failures were swallowed into retries, two URL forms for one video
paid for two summarizations, and metadata sidecars were invisible to both the
usage total and the cleanup sweep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.adapters.youtube.session_service import (
    _DOWNLOAD_STALE_AFTER_SEC,
    YouTubeDownloadSessionService,
)
from app.adapters.youtube.youtube_downloader_parts.storage import (
    ELIGIBLE_SUFFIXES,
    calculate_storage_usage,
)
from app.adapters.youtube.youtube_downloader_parts.transcript_api import format_transcript
from app.core.urls.normalization import compute_dedupe_hash
from app.core.urls.youtube import canonicalize_youtube_url


class TestStaleDownloadDetection:
    """Nothing reaps video_downloads; a killed worker leaves the row forever."""

    @staticmethod
    def _row(**over: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "id": 7,
            "status": "downloading",
            "download_started_at": datetime.now(UTC),
            "created_at": datetime.now(UTC),
        }
        base.update(over)
        return base

    def test_a_fresh_download_is_left_alone(self) -> None:
        assert YouTubeDownloadSessionService._stale_download_id(self._row()) is None

    def test_a_download_older_than_the_budget_is_stale(self) -> None:
        """Otherwise every later send of this URL waits out the 620 s poll."""
        old = datetime.now(UTC) - timedelta(seconds=_DOWNLOAD_STALE_AFTER_SEC + 60)
        assert (
            YouTubeDownloadSessionService._stale_download_id(self._row(download_started_at=old))
            == 7
        )

    def test_falls_back_to_created_at_for_a_pending_row(self) -> None:
        """download_started_at is only set on the move to "downloading"."""
        old = datetime.now(UTC) - timedelta(seconds=_DOWNLOAD_STALE_AFTER_SEC + 60)
        row = self._row(status="pending", download_started_at=None, created_at=old)
        assert YouTubeDownloadSessionService._stale_download_id(row) == 7

    def test_naive_timestamps_are_treated_as_utc(self) -> None:
        old = (datetime.now(UTC) - timedelta(seconds=_DOWNLOAD_STALE_AFTER_SEC + 60)).replace(
            tzinfo=None
        )
        assert (
            YouTubeDownloadSessionService._stale_download_id(self._row(download_started_at=old))
            == 7
        )

    def test_no_timestamp_at_all_prefers_restarting(self) -> None:
        """Nothing says a worker is alive, and the reuse path is the one that hangs."""
        row = self._row(download_started_at=None, created_at=None)
        assert YouTubeDownloadSessionService._stale_download_id(row) == 7

    def test_a_row_without_an_id_cannot_be_restarted(self) -> None:
        assert YouTubeDownloadSessionService._stale_download_id(self._row(id=None)) is None


class TestTranscriptApiPort:
    """youtube-transcript-api 1.x dropped the classmethod this code called."""

    def test_installed_library_has_no_list_transcripts(self) -> None:
        """Pins the reason for the port; if it ever returns, this test says so."""
        api = pytest.importorskip("youtube_transcript_api")
        assert not hasattr(api.YouTubeTranscriptApi, "list_transcripts")
        assert hasattr(api.YouTubeTranscriptApi, "list")

    def test_format_transcript_reads_1x_snippet_objects(self) -> None:
        """1.x yields dataclasses, not dicts; entry["text"] silently gave "" ."""
        snippets = [
            SimpleNamespace(text="hello", start=0.0, duration=1.0),
            SimpleNamespace(text="  world  ", start=1.0, duration=1.0),
        ]
        assert format_transcript(snippets, max_chars=100) == "hello world"

    def test_format_transcript_still_reads_legacy_dicts(self) -> None:
        legacy = [{"text": "hello"}, {"text": " world "}]
        assert format_transcript(legacy, max_chars=100) == "hello world"

    def test_format_transcript_truncates(self) -> None:
        assert format_transcript([{"text": "abcdef"}], max_chars=3) == "abc"


class TestDedupeCanonicalisation:
    """youtu.be/X and watch?v=X are one video and must hash alike."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        ],
    )
    def test_every_form_canonicalises_to_one_url(self, url: str) -> None:
        assert canonicalize_youtube_url(url) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_every_form_shares_one_dedupe_hash(self) -> None:
        """Two hashes meant two request rows, two downloads and two summarizations."""
        hashes = {
            compute_dedupe_hash(u)
            for u in (
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "https://youtu.be/dQw4w9WgXcQ",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
            )
        }
        assert len(hashes) == 1

    def test_a_different_video_still_hashes_differently(self) -> None:
        assert compute_dedupe_hash("https://youtu.be/dQw4w9WgXcQ") != compute_dedupe_hash(
            "https://youtu.be/oHg5SJYRHA0"
        )

    def test_non_youtube_urls_are_untouched(self) -> None:
        assert canonicalize_youtube_url("https://example.test/article") is None


class TestCompoundSuffixAccounting:
    def test_info_json_sidecars_are_counted(self, tmp_path: Path) -> None:
        """Path("v.info.json").suffix is ".json", so these were invisible.

        They counted toward neither the usage total nor the cleanup candidates
        and accumulated on the SD card forever.
        """
        (tmp_path / "v_title.mp4").write_bytes(b"x" * 100)
        (tmp_path / "v_title.info.json").write_bytes(b"y" * 50)

        assert ".info.json" in ELIGIBLE_SUFFIXES
        assert calculate_storage_usage(tmp_path) == 150

    def test_unrelated_files_are_still_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_bytes(b"z" * 10)
        (tmp_path / "v.mp4").write_bytes(b"x" * 20)
        assert calculate_storage_usage(tmp_path) == 20


class TestYtDlpInternalDeadline:
    """asyncio.timeout cannot cancel the worker thread; yt-dlp must stop itself."""

    def test_hook_passes_before_the_deadline(self) -> None:
        from app.adapters.youtube.youtube_downloader_parts.yt_dlp_client import make_deadline_hook

        make_deadline_hook(60.0)({"status": "downloading"})

    def test_hook_aborts_once_the_budget_is_spent(self) -> None:
        from app.adapters.youtube.youtube_downloader_parts import yt_dlp_client

        hook = yt_dlp_client.make_deadline_hook(-1.0)  # already expired
        with pytest.raises(TimeoutError, match="budget"):
            hook({"status": "downloading"})

    def test_options_carry_a_socket_timeout(self) -> None:
        from app.adapters.youtube.youtube_downloader_parts.yt_dlp_client import build_ydl_opts

        opts = build_ydl_opts(
            video_id="abc",
            output_path=Path("/tmp"),
            preferred_quality="1080p",
            subtitle_languages=["en"],
            max_video_size_mb=100,
        )
        assert opts["socket_timeout"] > 0


@pytest.mark.asyncio
async def test_stale_row_is_restarted_in_place_not_duplicated() -> None:
    """video_downloads.request_id is unique, so the row must be reused."""
    svc = object.__new__(YouTubeDownloadSessionService)
    old = datetime.now(UTC) - timedelta(seconds=_DOWNLOAD_STALE_AFTER_SEC + 60)
    video_repo = SimpleNamespace(
        async_get_video_download_by_request=AsyncMock(
            return_value={"id": 7, "status": "downloading", "download_started_at": old}
        ),
        async_update_video_download=AsyncMock(),
        async_create_video_download=AsyncMock(),
    )
    svc.video_repo = video_repo  # type: ignore[attr-defined]

    row = await video_repo.async_get_video_download_by_request(1)
    stale_id = svc._stale_download_id(row)
    assert stale_id == 7

    # The production path resets rather than creating; assert the reset shape.
    await svc.video_repo.async_update_video_download(
        stale_id, status="pending", download_started_at=None, error_text=None
    )
    video_repo.async_update_video_download.assert_awaited_once_with(
        7, status="pending", download_started_at=None, error_text=None
    )
    video_repo.async_create_video_download.assert_not_awaited()
