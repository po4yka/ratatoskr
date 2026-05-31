"""Attachment processor for images and PDFs sent to the bot."""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING, Any

from app.adapters.attachment._attachment_content import AttachmentContentService
from app.adapters.attachment._attachment_llm import AttachmentLLMWorkflowService
from app.adapters.attachment._attachment_persistence import AttachmentPersistenceService
from app.adapters.attachment._attachment_shared import AttachmentProcessorContext
from app.adapters.attachment.media_group_collector import MediaGroupCollector
from app.adapters.telegram.multimodal_extractor import build_telegram_summary_context
from app.application.services.summarization.llm_response_workflow import LLMResponseWorkflow
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.llm.protocol import LLMClientProtocol
    from app.application.ports.requests import LLMRepositoryPort, RequestRepositoryPort
    from app.application.ports.summaries import SummaryRepositoryPort
    from app.application.ports.users import UserRepositoryPort
    from app.config import AppConfig
    from app.db.session import Database
    from app.db.write_queue import DbWriteQueue

logger = get_logger(__name__)
_IMAGE_BUNDLE_TYPES = frozenset({"image"})


class AttachmentProcessor:
    """Processes image and PDF attachments sent to the bot."""

    def __init__(
        self,
        cfg: AppConfig,
        db: Database,
        openrouter: LLMClientProtocol,
        response_formatter: ResponseFormatter,
        audit_func: Callable[[str, str, dict], None],
        sem: Callable[[], Any],
        db_write_queue: DbWriteQueue | None = None,
        request_repo: RequestRepositoryPort | None = None,
        summary_repo: SummaryRepositoryPort | None = None,
        llm_repo: LLMRepositoryPort | None = None,
        user_repo: UserRepositoryPort | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.openrouter = openrouter
        self.response_formatter = response_formatter
        self._audit = audit_func
        self._sem = sem
        if request_repo is None:
            msg = "request_repo must be provided by the DI layer"
            raise ValueError(msg)
        if summary_repo is None:
            msg = "summary_repo must be provided by the DI layer"
            raise ValueError(msg)
        if llm_repo is None:
            msg = "llm_repo must be provided by the DI layer"
            raise ValueError(msg)
        if user_repo is None:
            msg = "user_repo must be provided by the DI layer"
            raise ValueError(msg)
        self.request_repo = request_repo
        self.user_repo = user_repo
        self._workflow = LLMResponseWorkflow(
            cfg=cfg,
            db=db,
            openrouter=openrouter,
            response_formatter=response_formatter,
            audit_func=audit_func,
            sem=sem,
            db_write_queue=db_write_queue,
            summary_repo=summary_repo,
            request_repo=request_repo,
            llm_repo=llm_repo,
            user_repo=user_repo,
        )
        self._context = AttachmentProcessorContext(
            cfg=cfg,
            db=db,
            openrouter=openrouter,
            response_formatter=response_formatter,
            audit_func=audit_func,
            sem=sem,
            request_repo=self.request_repo,
            user_repo=self.user_repo,
            workflow=self._workflow,
            logger=logger,
        )
        self._persistence = AttachmentPersistenceService(self._context)
        self._llm = AttachmentLLMWorkflowService(self._context)
        self._content = AttachmentContentService(
            self._context,
            persistence=self._persistence,
            workflow=self._llm,
        )
        self._media_group_collector: MediaGroupCollector[Any] = MediaGroupCollector()

    async def handle_attachment_flow(
        self,
        message: Any,
        *,
        correlation_id: str | None = None,
        interaction_id: int | None = None,
    ) -> None:
        """Main entry point for processing an attachment message."""
        from app.utils.progress_tracker import ProgressTracker

        file_path: str | None = None
        file_paths: list[str] = []
        progress_tracker: ProgressTracker | None = None
        progress_task: asyncio.Task[Any] | None = None
        current_status_text = ""

        async def progress_formatter(current: int, total: int, msg_id: int | None) -> int | None:
            del current, total
            if not msg_id:
                return msg_id
            chat_id = getattr(message.chat, "id", None)
            if not chat_id:
                return msg_id
            await self.response_formatter.edit_message(
                chat_id,
                msg_id,
                current_status_text,
                parse_mode="HTML",
            )
            return msg_id

        try:
            media_group_messages = await self._collect_media_group_messages(message)
            if media_group_messages is None:
                return

            file_type, mime_type, file_name = self._content.classify_attachment(message)
            if not file_type:
                await self.response_formatter.safe_reply(
                    message,
                    "This file type is not yet supported. Supported: images, PDFs, Office docs (docx/pptx/xlsx), EPUB.",
                )
                return

            size_error = self._content.check_size_limits(message, file_type)
            if size_error:
                await self.response_formatter.safe_reply(message, size_error)
                return

            type_label = {"image": "image", "document": "document"}.get(file_type, "PDF document")
            current_status_text = f"📥 <b>Processing {type_label}...</b>"
            progress_msg_id = await self.response_formatter.safe_reply_with_id(
                message,
                current_status_text,
                parse_mode="HTML",
            )
            if progress_msg_id:
                progress_tracker = ProgressTracker(
                    total=1,
                    progress_formatter=progress_formatter,
                    initial_message_id=progress_msg_id,
                    update_interval=0.5,
                )
                progress_task = asyncio.create_task(progress_tracker.process_update_queue())

            async def status_updater(text: str) -> None:
                nonlocal current_status_text
                current_status_text = text
                if progress_tracker:
                    progress_tracker.force_update()

            if progress_tracker:
                await status_updater(f"📥 <b>Downloading {type_label}...</b>")

            file_path = await self._content.download_attachment(message)
            if not file_path:
                await self.response_formatter.send_error_notification(
                    message,
                    "processing_failed",
                    correlation_id or "unknown",
                    details="Failed to download attachment",
                )
                return

            caption = self._build_summary_caption(media_group_messages)
            if len(media_group_messages) > 1 and file_type in _IMAGE_BUNDLE_TYPES:
                file_paths.append(file_path)
                for extra_message in media_group_messages[1:]:
                    extra_path = await self._content.download_attachment(extra_message)
                    if extra_path:
                        file_paths.append(extra_path)
                req_id, result = await self._content.process_downloaded_attachment_bundle(
                    message=message,
                    file_paths=file_paths,
                    caption=caption,
                    correlation_id=correlation_id,
                    interaction_id=interaction_id,
                    status_updater=status_updater,
                )
            else:
                req_id, result = await self._content.process_downloaded_attachment(
                    message=message,
                    file_path=file_path,
                    file_type=file_type,
                    mime_type=mime_type,
                    file_name=file_name,
                    caption=caption,
                    correlation_id=correlation_id,
                    interaction_id=interaction_id,
                    status_updater=status_updater,
                )

            if result:
                await self._persistence.update_attachment_status(req_id, "completed", result)
                await self._persistence.send_attachment_result(
                    message,
                    result,
                    req_id,
                    interaction_id,
                )
        except Exception as exc:
            logger.exception(
                "attachment_flow_error",
                extra={"error": str(exc), "cid": correlation_id},
            )
            try:
                await self.response_formatter.send_error_notification(
                    message,
                    "processing_failed",
                    correlation_id or "unknown",
                )
            except Exception:
                logger.warning(
                    "attachment_error_notification_failed",
                    extra={"cid": correlation_id},
                )
        finally:
            await self._complete_progress(
                progress_tracker,
                progress_task,
                suppress_task_errors=True,
            )
            cleanup_paths = [path for path in {file_path, *file_paths} if path]
            for path in cleanup_paths:
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError as exc:
                        logger.warning(
                            "attachment_cleanup_failed",
                            extra={"path": path, "error": str(exc)},
                        )

    async def _collect_media_group_messages(self, message: Any) -> list[Any] | None:
        media_group_id = getattr(message, "media_group_id", None)
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if not media_group_id or chat_id is None:
            return [message]
        return await self._media_group_collector.collect((chat_id, str(media_group_id)), message)

    @staticmethod
    def _build_summary_caption(messages: list[Any]) -> str | None:
        return build_telegram_summary_context(messages)

    async def _complete_progress(
        self,
        progress_tracker: Any | None,
        progress_task: asyncio.Task[Any] | None,
        *,
        suppress_task_errors: bool = False,
    ) -> None:
        """Stop progress updates and await the background progress task."""
        if progress_tracker:
            progress_tracker.mark_complete()
        if not progress_task:
            return
        if suppress_task_errors:
            with contextlib.suppress(Exception):
                await progress_task
            return
        await progress_task
