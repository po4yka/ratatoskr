"""CPU- and disk-bound work must not run on the event loop thread.

Each of these sits next to a sibling call that was already offloaded, which is
what makes them oversights rather than choices: the PDF and markitdown branches
of AttachmentContentService use asyncio.to_thread, auto_cleanup_storage is
wrapped while the usage scan around it was not, and every writer call in the AI
backup clients is wrapped while the matching read was not.

The assertion is the thread identity rather than elapsed time, so these stay
deterministic instead of racing a timer.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.attachment._attachment_content import AttachmentContentService
from app.adapters.attachment.image_extractor import ImageContent

from .test_attachment_document_messages import _make_context


def _image_content() -> ImageContent:
    return ImageContent(
        data_uri="data:image/jpeg;base64,AAAA",
        mime_type="image/jpeg",
        width=10,
        height=10,
        file_size_bytes=4,
    )


class TestImageExtractionOffloading:
    """Image.open + LANCZOS resize + JPEG encode is seconds of CPU on a big photo."""

    @staticmethod
    def _service() -> AttachmentContentService:
        ctx = _make_context()
        ctx.cfg.attachment.image_max_dimension = 2048
        ctx.response_formatter.safe_reply = AsyncMock()
        svc = AttachmentContentService(ctx, persistence=MagicMock(), workflow=MagicMock())
        svc._workflow.run_summary_workflow = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_single_image_extract_leaves_the_loop_thread(self) -> None:
        loop_thread = threading.get_ident()
        seen: list[int] = []

        def _record(*_args: Any, **_kwargs: Any) -> ImageContent:
            seen.append(threading.get_ident())
            return _image_content()

        with mock.patch(
            "app.adapters.attachment._attachment_content.ImageExtractor.extract",
            side_effect=_record,
        ):
            await self._service().process_image(
                file_path="/tmp/photo.jpg",
                caption=None,
                chosen_lang="en",
                req_id=1,
                correlation_id="cid",
                interaction_id=None,
                message=MagicMock(),
            )

        assert seen, "the extractor never ran"
        assert seen[0] != loop_thread, "image decode ran on the event loop thread"

    @pytest.mark.asyncio
    async def test_bundle_extract_leaves_the_loop_thread(self) -> None:
        """An album is up to ten images; ten synchronous decodes froze the bot."""
        loop_thread = threading.get_ident()
        seen: list[int] = []

        def _record(*_args: Any, **_kwargs: Any) -> ImageContent:
            seen.append(threading.get_ident())
            return _image_content()

        with mock.patch(
            "app.adapters.attachment._attachment_content.ImageExtractor.extract",
            side_effect=_record,
        ):
            await self._service().process_image_bundle(
                file_paths=["/tmp/a.jpg", "/tmp/b.jpg", "/tmp/c.jpg"],
                caption=None,
                chosen_lang="en",
                req_id=1,
                correlation_id="cid",
                interaction_id=None,
                message=MagicMock(),
            )

        assert len(seen) == 3
        assert all(t != loop_thread for t in seen), "an image decoded on the event loop thread"


class TestYouTubeStorageScanOffloading:
    """Recursive rglob plus a stat() per file, on every YouTube request."""

    @pytest.mark.asyncio
    async def test_storage_usage_scan_leaves_the_loop_thread(self, tmp_path: Path) -> None:
        from app.adapters.youtube.session_service import YouTubeDownloadSessionService

        svc = object.__new__(YouTubeDownloadSessionService)
        svc._cfg = SimpleNamespace(
            youtube=SimpleNamespace(
                max_storage_gb=1,
                auto_cleanup_enabled=False,
                storage_path=str(tmp_path),
            )
        )
        svc.storage_path = tmp_path

        loop_thread = threading.get_ident()
        seen: list[int] = []

        def _scan() -> int:
            seen.append(threading.get_ident())
            return 0

        svc.calculate_storage_usage = _scan  # type: ignore[method-assign]

        await svc.check_storage_limits()

        assert seen, "the scan never ran"
        assert seen[0] != loop_thread, "the storage scan ran on the event loop thread"


class TestAiBackupResumeReadOffloading:
    """One JSON read per conversation, inside a loop over the whole account."""

    @pytest.mark.parametrize("module_name", ["chatgpt_client", "claude_client"])
    def test_saved_conversation_read_is_offloaded(self, module_name: str) -> None:
        """Assert on the source: driving the full account walk needs live HTTP.

        The matching writer calls in both files are already wrapped, so a bare
        call here is the outlier, not the convention.
        """
        import importlib

        module = importlib.import_module(f"app.adapters.ai_backup.{module_name}")
        source = Path(module.__file__ or "").read_text()
        for line in source.splitlines():
            if "load_saved_conversation" not in line:
                continue
            assert "to_thread" in line, (
                f"{module_name}: load_saved_conversation runs on the event loop: {line.strip()}"
            )


@pytest.mark.asyncio
async def test_to_thread_actually_uses_a_different_thread() -> None:
    """Guards the assertion the tests above rely on."""
    loop_thread = threading.get_ident()
    worker_thread = await asyncio.to_thread(threading.get_ident)
    assert worker_thread != loop_thread
