"""IO-heavy callback handlers for Telegram inline actions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.core.ui_strings import t

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.callback_action_presenters import CallbackActionPresenters
    from app.adapters.telegram.callback_action_store import CallbackActionStore
    from app.adapters.telegram.url_handler import URLHandler
    from app.db.session import Database

logger = get_logger(__name__)


class CallbackActionIOHandlers:
    """Handle callback actions that hit the DB, filesystem, or external services."""

    def __init__(
        self,
        *,
        db: Database,
        response_formatter: ResponseFormatter,
        store: CallbackActionStore,
        presenters: CallbackActionPresenters,
        url_handler: URLHandler | None = None,
        lang: str = "en",
        digest_timeout: float = 180.0,
        export_timeout: float = 60.0,
        llm_timeout: float = 120.0,
        asyncio_module: Any = asyncio,
    ) -> None:
        self._db = db
        self._response_formatter = response_formatter
        self._store = store
        self._presenters = presenters
        self._url_handler = url_handler
        self._lang = lang
        self._digest_timeout = digest_timeout
        self._export_timeout = export_timeout
        self._llm_timeout = llm_timeout
        self._asyncio = asyncio_module

    async def handle_digest_full_summary(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        if len(parts) < 3:
            logger.warning("digest_callback_missing_params", extra={"parts": parts})
            return False

        try:
            channel_id = int(parts[1])
            msg_id = int(parts[2])
        except (ValueError, IndexError):
            logger.warning("digest_callback_invalid_params", extra={"parts": parts})
            await self._response_formatter.safe_reply(message, "Invalid digest callback data.")
            return True

        post = await self._store.get_digest_post(channel_id, msg_id)

        if not post:
            await self._response_formatter.safe_reply(message, "Post not found in database.")
            return True

        await self._response_formatter.safe_reply(message, t("cb_generating_summary", self._lang))

        post_url = post.url or ""
        if post_url and self._url_handler:
            try:
                await self._asyncio.wait_for(
                    self._url_handler.handle_single_url(
                        message=message,
                        url=post_url,
                        correlation_id=correlation_id,
                        interaction_id=0,
                    ),
                    timeout=self._digest_timeout,
                )
            except TimeoutError:
                logger.warning(
                    "digest_full_summary_timeout",
                    extra={"cid": correlation_id, "timeout": self._digest_timeout},
                )
                await self._response_formatter.safe_reply(message, t("cb_timeout", self._lang))
            except Exception as exc:
                logger.exception(
                    "digest_full_summary_failed",
                    extra={"cid": correlation_id, "error": str(exc)},
                )
                await self._send_digest_post_fallback(message, post, post_url)
        else:
            await self._send_digest_post_fallback(message, post, post_url)

        logger.info(
            "digest_full_summary_sent",
            extra={
                "channel_id": channel_id,
                "message_id": msg_id,
                "uid": uid,
                "cid": correlation_id,
            },
        )
        return True

    async def _send_digest_post_fallback(self, message: Any, post: Any, post_url: str) -> None:
        reply_text = self._presenters.format_digest_post_fallback(post, post_url)
        await self._response_formatter.safe_reply(message, reply_text)

    async def handle_export(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        if len(parts) < 3:
            logger.warning("export_missing_params", extra={"parts": parts, "cid": correlation_id})
            return False

        summary_id = ":".join(parts[1:-1]).strip()
        export_format = parts[-1].lower()
        if not summary_id:
            logger.warning(
                "export_missing_summary_id",
                extra={"parts": parts, "cid": correlation_id},
            )
            return False

        if export_format not in ("pdf", "md", "html", "json"):
            await self._response_formatter.safe_reply(
                message,
                f"Unknown export format: {export_format}",
            )
            return True

        # Defence-in-depth IDOR guard: summary_id comes straight from
        # callback_data, so verify the requesting user owns this summary before
        # exporting it. Reply "not found" (not "denied") so a non-owner cannot
        # confirm the summary exists.
        if not await self._store.summary_belongs_to_user(summary_id, uid):
            logger.warning(
                "export_access_denied",
                extra={"uid": uid, "summary_id": summary_id, "cid": correlation_id},
            )
            await self._response_formatter.safe_reply(
                message,
                t("cb_summary_not_found", self._lang),
            )
            return True

        if export_format == "json":
            return await self._handle_json_export(message, summary_id, correlation_id)

        from app.adapters.external.formatting.export_formatter import ExportFormatter

        exporter = ExportFormatter(self._db)

        try:
            await self._response_formatter.safe_reply(
                message,
                t("cb_export_generating", self._lang).format(fmt=export_format.upper()),
            )

            file_path, filename = await self._asyncio.wait_for(
                self._asyncio.to_thread(
                    exporter.export_summary,
                    summary_id=summary_id,
                    export_format=export_format,
                    correlation_id=correlation_id,
                ),
                timeout=self._export_timeout,
            )

            if file_path and filename:
                await self._send_file(message, file_path, filename, export_format)
                logger.info(
                    "export_completed",
                    extra={
                        "format": export_format,
                        "summary_id": summary_id,
                        "cid": correlation_id,
                    },
                )
            else:
                await self._response_formatter.safe_reply(
                    message,
                    t("cb_export_failed", self._lang).format(cid=correlation_id),
                )
        except TimeoutError:
            logger.warning(
                "export_timeout",
                extra={"format": export_format, "summary_id": summary_id, "cid": correlation_id},
            )
            await self._response_formatter.safe_reply(message, t("cb_timeout", self._lang))
        except Exception as exc:
            logger.exception(
                "export_failed",
                extra={"format": export_format, "summary_id": summary_id, "error": str(exc)},
            )
            await self._response_formatter.safe_reply(
                message,
                f"Export failed: {type(exc).__name__}. Error ID: {correlation_id}",
            )

        return True

    async def _handle_json_export(
        self,
        message: Any,
        summary_id: str,
        correlation_id: str,
    ) -> bool:
        """Export summary as a JSON file attachment."""
        import io
        import json
        from datetime import UTC, datetime

        payload = await self._store.load_summary_payload(summary_id, correlation_id=correlation_id)
        if not payload:
            await self._response_formatter.safe_reply(
                message,
                t("cb_export_failed", self._lang).format(cid=correlation_id),
            )
            return True

        try:
            pretty = json.dumps(payload, ensure_ascii=False, indent=2)
            bio = io.BytesIO(pretty.encode("utf-8"))
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            bio.name = f"summary-{summary_id}-{ts}.json"

            if hasattr(message, "reply_document"):
                await message.reply_document(bio, caption="Full Summary JSON")
            else:
                await self._response_formatter.safe_reply(
                    message,
                    f"JSON ready but unable to send as document. Error ID: {correlation_id}",
                )
        except Exception as exc:
            from app.core.async_utils import raise_if_cancelled

            raise_if_cancelled(exc)
            logger.exception(
                "json_export_failed",
                extra={"summary_id": summary_id, "error": str(exc), "cid": correlation_id},
            )
            await self._response_formatter.safe_reply(
                message,
                f"Export failed: {type(exc).__name__}. Error ID: {correlation_id}",
            )
            return True

        logger.info(
            "export_completed",
            extra={"format": "json", "summary_id": summary_id, "cid": correlation_id},
        )
        return True

    async def _send_file(
        self,
        message: Any,
        file_path: str,
        filename: str,
        export_format: str,
    ) -> None:
        from pathlib import Path

        path = Path(file_path)
        if not path.exists():
            await self._response_formatter.safe_reply(message, "Export file not found.")
            return

        caption_map = {
            "pdf": "PDF export",
            "md": "Markdown export",
            "html": "HTML export",
            "json": "JSON export",
        }
        caption = caption_map.get(export_format, "Exported file")

        try:
            if hasattr(message, "reply_document"):
                await message.reply_document(str(path), caption=caption)
            else:
                await self._response_formatter.safe_reply(
                    message,
                    f"File ready: {filename} (unable to send as document)",
                )
        finally:
            try:
                path.unlink()
            except Exception as exc:
                logger.debug("temp_file_cleanup_failed", extra={"error": str(exc)})

    async def handle_retry(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        if len(parts) < 2:
            logger.warning("retry_callback_missing_cid", extra={"parts": parts})
            return False

        original_cid = parts[1]
        url = await self._store.lookup_retry_url(original_cid, uid)
        if not url:
            logger.warning(
                "retry_url_not_found",
                extra={"original_cid": original_cid, "uid": uid, "cid": correlation_id},
            )
            await self._response_formatter.safe_reply(
                message,
                t("cb_retry_url_not_found", self._lang),
            )
            return True

        if not self._url_handler:
            logger.error("retry_no_url_handler", extra={"cid": correlation_id})
            await self._response_formatter.safe_reply(
                message, t("cb_retry_unavailable", self._lang)
            )
            return True

        await self._response_formatter.safe_reply(message, t("cb_retrying", self._lang))

        try:
            await self._asyncio.wait_for(
                self._url_handler.handle_single_url(
                    message=message,
                    url=url,
                    correlation_id=correlation_id,
                ),
                timeout=self._llm_timeout,
            )
        except TimeoutError:
            logger.warning(
                "retry_timeout",
                extra={"cid": correlation_id, "url": url, "timeout": self._llm_timeout},
            )
            await self._response_formatter.safe_reply(message, t("cb_timeout", self._lang))
        except Exception as exc:
            logger.exception(
                "retry_failed",
                extra={"cid": correlation_id, "url": url, "error": str(exc)},
            )
            await self._response_formatter.send_error_notification(
                message,
                "processing_failed",
                correlation_id,
            )

        return True
