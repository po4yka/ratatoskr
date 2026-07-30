"""Attachment size limits, filename capture, and terminal status.

`check_size_limits` read a flat `file_size` that no real Telethon message has:
a raw Document exposes `size` and a raw Photo only `sizes`, so the probe always
returned None, took the early return, and ATTACHMENT_MAX_*_SIZE_MB was never
enforced. The same wrong attribute left attachment_processing.file_name NULL.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.attachment._attachment_content import AttachmentContentService
from app.adapters.telegram_source_helpers import telegram_media_file_name, telegram_media_size

from .test_attachment_document_messages import _make_context

pytest.importorskip("telethon")


def _telethon_document_message(size: int, name: str = "report.pdf") -> Any:
    """A message shaped the way Telethon actually delivers a document."""
    import datetime

    from telethon.tl.types import Document, DocumentAttributeFilename

    doc = Document(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        mime_type="application/pdf",
        size=size,
        dc_id=2,
        attributes=[DocumentAttributeFilename(file_name=name)],
    )
    # The File helper is what exposes a uniform .size/.name.
    return SimpleNamespace(
        document=doc,
        photo=None,
        file=SimpleNamespace(size=size, name=name),
    )


class TestSizeProbe:
    def test_reads_the_size_off_a_real_telethon_document(self) -> None:
        assert telegram_media_size(_telethon_document_message(5_000_000)) == 5_000_000

    def test_reads_the_size_off_a_photo_via_the_file_helper(self) -> None:
        """A raw Photo has no flat size at all -- only `sizes`."""
        message = SimpleNamespace(
            photo=SimpleNamespace(sizes=[]), document=None, file=SimpleNamespace(size=900_000)
        )
        assert telegram_media_size(message) == 900_000

    def test_still_reads_aiogram_shaped_messages(self) -> None:
        message = SimpleNamespace(photo=SimpleNamespace(file_size=1234), document=None, file=None)
        assert telegram_media_size(message) == 1234

    def test_unknown_shape_reports_none(self) -> None:
        assert telegram_media_size(SimpleNamespace(photo=None, document=None, file=None)) is None

    def test_filename_comes_off_the_file_helper(self) -> None:
        assert telegram_media_file_name(_telethon_document_message(1, "paper.pdf")) == "paper.pdf"


class TestSizeLimitEnforcement:
    @staticmethod
    def _service(max_pdf_mb: int = 10) -> AttachmentContentService:
        ctx = _make_context()
        ctx.cfg.attachment.max_pdf_size_mb = max_pdf_mb
        ctx.cfg.attachment.max_image_size_mb = max_pdf_mb
        ctx.cfg.attachment.max_document_size_mb = max_pdf_mb
        return AttachmentContentService(ctx, persistence=MagicMock(), workflow=MagicMock())

    def test_oversize_telethon_document_is_refused(self) -> None:
        """Previously downloaded and parsed in-process, whatever its size."""
        message = _telethon_document_message(50 * 1024 * 1024)

        refusal = self._service(max_pdf_mb=10).check_size_limits(message, "pdf")

        assert refusal is not None
        assert "10" in refusal

    def test_document_within_the_limit_passes(self) -> None:
        message = _telethon_document_message(2 * 1024 * 1024)
        assert self._service(max_pdf_mb=10).check_size_limits(message, "pdf") is None

    def test_classify_attachment_captures_the_filename(self) -> None:
        """attachment_processing.file_name was NULL for every upload."""
        message = _telethon_document_message(1024, "quarterly.pdf")

        file_type, mime, fname = self._service().classify_attachment(message)

        assert file_type == "pdf"
        assert mime == "application/pdf"
        assert fname == "quarterly.pdf"


class TestTerminalStatus:
    """`status` is the only discriminator between finished and failed."""

    @pytest.mark.asyncio
    async def test_failure_path_records_a_terminal_status(self) -> None:
        from app.adapters.attachment.attachment_processor import AttachmentProcessor

        proc = object.__new__(AttachmentProcessor)
        persistence = MagicMock()
        persistence.update_attachment_status = AsyncMock()
        proc._persistence = persistence

        await proc._mark_attachment_failed(42, "cid-1")

        persistence.update_attachment_status.assert_awaited_once_with(42, "failed", None)

    @pytest.mark.asyncio
    async def test_a_failing_status_write_does_not_raise(self) -> None:
        """This runs on the error path; it must not mask the original failure."""
        from app.adapters.attachment.attachment_processor import AttachmentProcessor

        proc = object.__new__(AttachmentProcessor)
        persistence = MagicMock()
        persistence.update_attachment_status = AsyncMock(side_effect=RuntimeError("db down"))
        proc._persistence = persistence

        await proc._mark_attachment_failed(42, "cid-1")
