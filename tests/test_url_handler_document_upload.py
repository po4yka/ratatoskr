"""Tests for uploaded .txt / .md document handling in URLHandler.

Covers the content-sniff that decides whether an uploaded text document is
summarized as an article (Markdown always, prose .txt) or processed as a batch
list of URLs (.txt that is predominantly bare links).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from app.adapters.telegram.url_handler import URLHandler
from app.domain.models.request import RequestType

_SLEEP_TARGET = "app.adapters.telegram.url_handler.asyncio.sleep"


def _make_handler(*, lines: list[str], summary_success: bool = True) -> URLHandler:
    response_formatter = cast(
        "Any",
        SimpleNamespace(
            safe_reply=AsyncMock(),
            safe_reply_with_id=AsyncMock(return_value=42),
            send_error_notification=AsyncMock(),
            MIN_MESSAGE_INTERVAL_MS=100,
            MAX_BATCH_URLS=50,
            _validate_url=MagicMock(return_value=(True, None)),
            sender=SimpleNamespace(is_draft_streaming_enabled=MagicMock(return_value=False)),
        ),
    )
    url_processor = cast(
        "Any",
        SimpleNamespace(
            handle_url_flow=AsyncMock(),
            create_text_request=AsyncMock(return_value=123),
            summarize_text_request=AsyncMock(return_value=SimpleNamespace(success=summary_success)),
            summary_repo=None,
            audit_func=None,
        ),
    )
    file_validator = MagicMock()
    file_validator.cleanup_file = MagicMock()
    file_validator.safe_read_text_file = MagicMock(return_value=list(lines))
    return URLHandler(
        db=cast("Any", SimpleNamespace()),
        response_formatter=response_formatter,
        url_processor=url_processor,
        file_validator=file_validator,
    )


def _doc_message(file_name: str) -> Any:
    return SimpleNamespace(
        document=SimpleNamespace(file_name=file_name, mime_type="text/plain"),
        chat=SimpleNamespace(id=999),
        from_user=SimpleNamespace(id=555),
    )


class TestCanHandleDocument(unittest.TestCase):
    def test_accepts_txt_md_markdown_any_case(self) -> None:
        handler = _make_handler(lines=[])
        for name in ("notes.txt", "article.md", "post.markdown", "READ.MD", "x.TxT"):
            self.assertTrue(handler.can_handle_document(_doc_message(name)), name)

    def test_rejects_other_extensions_and_missing_document(self) -> None:
        handler = _make_handler(lines=[])
        for name in ("paper.pdf", "image.png", "data.csv", "noext"):
            self.assertFalse(handler.can_handle_document(_doc_message(name)), name)
        self.assertFalse(handler.can_handle_document(SimpleNamespace(document=None)))


class TestShouldSummarizeAsArticle(unittest.TestCase):
    def test_markdown_is_always_article_even_when_all_urls(self) -> None:
        handler = _make_handler(lines=[])
        msg = _doc_message("links.md")
        self.assertTrue(
            handler._should_summarize_as_article(
                msg, ["https://a.com", "https://b.com", "https://c.com"]
            )
        )

    def test_txt_all_urls_is_url_list(self) -> None:
        handler = _make_handler(lines=[])
        msg = _doc_message("links.txt")
        self.assertFalse(
            handler._should_summarize_as_article(msg, ["https://a.com", "https://b.com"])
        )

    def test_txt_prose_is_article(self) -> None:
        handler = _make_handler(lines=[])
        msg = _doc_message("essay.txt")
        prose = ["The quick brown fox", "jumps over the lazy dog.", "https://ref.com"]
        self.assertTrue(handler._should_summarize_as_article(msg, prose))

    def test_txt_empty_or_comment_only_defers_to_url_list(self) -> None:
        handler = _make_handler(lines=[])
        msg = _doc_message("empty.txt")
        self.assertFalse(handler._should_summarize_as_article(msg, ["", "  ", "# only a comment"]))

    def test_txt_half_urls_is_article(self) -> None:
        # ratio == 0.5 < 0.8 threshold -> article (prose is preserved, not dropped)
        handler = _make_handler(lines=[])
        msg = _doc_message("mixed.txt")
        lines = ["https://a.com", "https://b.com", "prose one", "prose two"]
        self.assertTrue(handler._should_summarize_as_article(msg, lines))

    def test_txt_overwhelmingly_urls_is_url_list(self) -> None:
        # ratio == 0.8 -> not below threshold -> batch URL list
        handler = _make_handler(lines=[])
        msg = _doc_message("links.txt")
        lines = [
            "https://a.com",
            "https://b.com",
            "https://c.com",
            "https://d.com",
            "a stray note",
        ]
        self.assertFalse(handler._should_summarize_as_article(msg, lines))


class TestHandleDocumentArticlePath(unittest.IsolatedAsyncioTestCase):
    async def test_markdown_summarized_as_article(self) -> None:
        handler = _make_handler(lines=["# Title", "", "Body paragraph with content."])
        message = _doc_message("article.md")

        with patch(
            "app.adapters.telegram.url_handler.URLHandler._download_document_file",
            new_callable=AsyncMock,
            return_value="/tmp/article.md",
        ):
            await handler.handle_document_file(message, "cid-1", 7, 0.0)

        proc = cast("Any", handler.url_processor)
        proc.create_text_request.assert_awaited_once()
        self.assertEqual(
            proc.create_text_request.await_args.kwargs["request_type"], RequestType.UPLOAD
        )
        proc.summarize_text_request.assert_awaited_once()
        kwargs = proc.summarize_text_request.await_args.kwargs
        self.assertEqual(kwargs["request_id"], 123)
        self.assertEqual(kwargs["request_type"], RequestType.UPLOAD)
        self.assertEqual(kwargs["interaction_id"], 7)
        self.assertIn("Body paragraph", kwargs["content_text"])
        cast("Any", handler.response_formatter).send_error_notification.assert_not_awaited()

    async def test_empty_file_replies_without_summarizing(self) -> None:
        handler = _make_handler(lines=["", "   ", ""])
        message = _doc_message("blank.md")

        with patch(
            "app.adapters.telegram.url_handler.URLHandler._download_document_file",
            new_callable=AsyncMock,
            return_value="/tmp/blank.md",
        ):
            await handler.handle_document_file(message, "cid-2", 1, 0.0)

        proc = cast("Any", handler.url_processor)
        proc.create_text_request.assert_not_awaited()
        proc.summarize_text_request.assert_not_awaited()
        rf = cast("Any", handler.response_formatter)
        rf.safe_reply.assert_awaited()
        rf.send_error_notification.assert_not_awaited()

    async def test_oversized_content_is_truncated(self) -> None:
        from app.adapters.telegram.url_handler import _MAX_UPLOAD_CONTENT_CHARS

        handler = _make_handler(lines=["x" * (_MAX_UPLOAD_CONTENT_CHARS + 50_000)])
        message = _doc_message("huge.md")

        with patch(
            "app.adapters.telegram.url_handler.URLHandler._download_document_file",
            new_callable=AsyncMock,
            return_value="/tmp/huge.md",
        ):
            await handler.handle_document_file(message, "cid-big", 1, 0.0)

        proc = cast("Any", handler.url_processor)
        kwargs = proc.summarize_text_request.await_args.kwargs
        self.assertEqual(len(kwargs["content_text"]), _MAX_UPLOAD_CONTENT_CHARS)

    async def test_summarize_exception_notifies_error(self) -> None:
        handler = _make_handler(lines=["Article body."])
        cast("Any", handler.url_processor).summarize_text_request = AsyncMock(
            side_effect=RuntimeError("graph blew up")
        )
        message = _doc_message("article.md")

        with patch(
            "app.adapters.telegram.url_handler.URLHandler._download_document_file",
            new_callable=AsyncMock,
            return_value="/tmp/article.md",
        ):
            await handler.handle_document_file(message, "cid-x", 1, 0.0)

        cast("Any", handler.response_formatter).send_error_notification.assert_awaited_once()

    async def test_failed_summary_notifies_error(self) -> None:
        handler = _make_handler(
            lines=["Real article body that should be summarized."], summary_success=False
        )
        message = _doc_message("article.md")

        with patch(
            "app.adapters.telegram.url_handler.URLHandler._download_document_file",
            new_callable=AsyncMock,
            return_value="/tmp/article.md",
        ):
            await handler.handle_document_file(message, "cid-3", 1, 0.0)

        cast("Any", handler.response_formatter).send_error_notification.assert_awaited_once()


class TestHandleDocumentUrlListPath(unittest.IsolatedAsyncioTestCase):
    async def test_txt_url_list_routes_to_batch(self) -> None:
        handler = _make_handler(lines=["https://a.com", "https://b.com"])
        message = _doc_message("links.txt")

        with (
            patch(
                "app.adapters.telegram.url_handler.URLHandler._download_document_file",
                new_callable=AsyncMock,
                return_value="/tmp/links.txt",
            ),
            patch(
                "app.adapters.telegram.url_handler.URLHandler.apply_url_security_checks",
                new_callable=AsyncMock,
                side_effect=lambda _m, urls, _u, _c: urls,
            ),
            patch(
                "app.adapters.telegram.url_handler.URLHandler.process_url_batch",
                new_callable=AsyncMock,
            ) as batch,
            patch(_SLEEP_TARGET, new_callable=AsyncMock),
        ):
            await handler.handle_document_file(message, "cid-4", 1, 0.0)

        batch.assert_awaited_once()
        cast("Any", handler.url_processor).create_text_request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
