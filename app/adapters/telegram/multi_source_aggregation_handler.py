# ruff: noqa: TC001
"""Telegram entrypoints for mixed-source aggregation bundles."""

from __future__ import annotations

import html
from collections import Counter
from typing import TYPE_CHECKING, Any

from app.adapters.attachment.media_group_collector import MediaGroupCollector
from app.application.dto.aggregation import (
    MultiSourceExtractionOutput,
    SourceSubmission,
)
from app.application.services.aggregation_rollout import AggregationRolloutGate
from app.application.services.multi_source_aggregation_service import (
    MultiSourceAggregationRunResult,
    MultiSourceAggregationService,
)
from app.core.logging_utils import get_logger
from app.core.url_utils import extract_all_urls

logger = get_logger(__name__)

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )


class MultiSourceAggregationHandler:
    """Handle Telegram commands and message routes for bundle aggregation."""

    def __init__(
        self,
        *,
        response_formatter: ResponseFormatter,
        workflow_service: MultiSourceAggregationService,
        rollout_gate: AggregationRolloutGate | None = None,
        lang: str = "en",
    ) -> None:
        self._response_formatter = response_formatter
        self._workflow_service = workflow_service
        self._rollout_gate = rollout_gate
        self._lang = lang
        self._media_group_collector: MediaGroupCollector[Any] = MediaGroupCollector()

    async def handle_command(
        self,
        *,
        message: Any,
        text: str,
        uid: int,
        correlation_id: str,
        interaction_id: int | None = None,
    ) -> bool:
        if not await self._ensure_enabled(uid=uid, message=message, explicit=True):
            return False
        submissions = await self._build_submissions(
            message=message,
            text=text,
            include_message_source=self._should_include_message_source(message),
        )
        if not submissions:
            await self._response_formatter.safe_reply(
                message,
                "Send at least one link, or provide a forwarded message or attachment to aggregate.",
            )
            return True

        await self.run_with_submissions(
            message=message,
            uid=uid,
            correlation_id=correlation_id,
            submissions=submissions,
            metadata={
                "entrypoint": "telegram_command",
                "interaction_id": interaction_id,
            },
        )
        return True

    async def handle_message_bundle(
        self,
        *,
        message: Any,
        text: str,
        uid: int,
        correlation_id: str,
        interaction_id: int | None = None,
    ) -> bool:
        if not await self._ensure_enabled(uid=uid, message=message, explicit=False):
            return False
        submissions = await self._build_submissions(
            message=message,
            text=text,
            include_message_source=self._should_include_message_source(message),
        )
        if len(submissions) < 2:
            return False

        await self.run_with_submissions(
            message=message,
            uid=uid,
            correlation_id=correlation_id,
            submissions=submissions,
            metadata={
                "entrypoint": "telegram_message",
                "interaction_id": interaction_id,
            },
        )
        return True

    async def ensure_enabled(
        self,
        *,
        uid: int,
        message: Any,
        explicit: bool,
    ) -> bool:
        """Public alias for :meth:`_ensure_enabled` so external callers (the
        coalescer) can run the rollout-gate check before deciding whether to
        synthesize a bundle or fall back to per-message dispatch."""
        return await self._ensure_enabled(uid=uid, message=message, explicit=explicit)

    async def build_submissions_for_message(
        self,
        *,
        message: Any,
        text: str,
        include_message_source: bool | None = None,
    ) -> list[SourceSubmission]:
        """Public wrapper around :meth:`_build_submissions`.

        Used by the coalescer to build the per-message submission list for
        each buffered Telegram message before concatenating across the bucket.
        """
        if include_message_source is None:
            include_message_source = self._should_include_message_source(message)
        return await self._build_submissions(
            message=message,
            text=text,
            include_message_source=include_message_source,
        )

    async def run_with_submissions(
        self,
        *,
        message: Any,
        uid: int,
        correlation_id: str,
        submissions: list[SourceSubmission],
        metadata: dict[str, Any],
    ) -> None:
        progress_state: Counter[str] = Counter()
        progress_message_id: int | None = None
        if not self._response_formatter.is_draft_streaming_enabled():
            progress_message_id = await self._response_formatter.safe_reply_with_id(
                message,
                self._render_progress(progress_state, len(submissions)),
                parse_mode="HTML",
            )

        async def _on_progress(event: dict[str, Any]) -> None:
            progress_state[event["event"]] += 1
            await self._update_progress(
                message=message,
                progress_message_id=progress_message_id,
                text=self._render_progress(progress_state, len(submissions)),
            )

        try:
            result = await self._workflow_service.aggregate(
                correlation_id=correlation_id,
                user_id=uid,
                submissions=submissions,
                language=self._lang,
                metadata=metadata,
                progress_callback=_on_progress,
            )
        except Exception:
            logger.exception(
                "aggregate_failed",
                extra={"cid": correlation_id, "submission_count": len(submissions)},
            )
            self._response_formatter.clear_message_draft(message)
            await self._response_formatter.send_error_notification(
                message,
                "processing_failed",
                correlation_id,
                details="Bundle aggregation failed before synthesis completed.",
            )
            return

        self._response_formatter.clear_message_draft(message)
        await self._update_progress(
            message=message,
            progress_message_id=progress_message_id,
            text=self._render_completed_progress(result),
        )
        await self._response_formatter.safe_reply(
            message,
            self._render_result(result),
            parse_mode="HTML",
        )

    async def _build_submissions(
        self,
        *,
        message: Any,
        text: str,
        include_message_source: bool,
    ) -> list[SourceSubmission]:
        submissions = [SourceSubmission.from_url(url) for url in extract_all_urls(text)]
        if include_message_source:
            message_submission = await self._build_message_submission(message)
            if message_submission is not None:
                submissions.append(message_submission)
        return submissions

    async def _build_message_submission(self, message: Any) -> SourceSubmission | None:
        media_group_id = getattr(message, "media_group_id", None)
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if media_group_id and chat_id is not None:
            messages = await self._media_group_collector.collect(
                (chat_id, str(media_group_id)), message
            )
            if messages:
                return SourceSubmission.from_telegram_messages(messages)
        return SourceSubmission.from_telegram_message(message)

    def _render_progress(self, counts: Counter[str], total_items: int) -> str:
        started = counts.get("item_processing", 0)
        extracted = counts.get("item_extracted", 0)
        failed = counts.get("item_failed", 0)
        duplicate = counts.get("item_duplicate", 0)
        return (
            "<b>Bundle aggregation in progress</b>\n"
            f"Sources: {total_items}\n"
            f"Started: {started}\n"
            f"Extracted: {extracted}\n"
            f"Failed: {failed}\n"
            f"Duplicates: {duplicate}"
        )

    def _render_completed_progress(self, result: MultiSourceAggregationRunResult) -> str:
        extraction = result.extraction
        aggregation = result.aggregation
        return (
            "<b>Bundle aggregation complete</b>\n"
            f"Used sources: {aggregation.used_source_count}/{extraction.successful_count}\n"
            f"Failures: {extraction.failed_count}\n"
            f"Duplicates: {extraction.duplicate_count}"
        )

    def _render_result(self, result: MultiSourceAggregationRunResult) -> str:
        extraction = result.extraction
        aggregation = result.aggregation
        source_index = {item.source_item_id: item.position + 1 for item in extraction.items}

        parts = [
            "<b>Bundle Summary</b>",
            f"<b>Session:</b> <code>{aggregation.session_id}</code>",
            f"<b>Source Type:</b> {html.escape(aggregation.source_type)}",
            "",
            html.escape(aggregation.overview),
        ]

        if aggregation.key_claims:
            parts.append("\n<b>Key Claims</b>")
            for claim in aggregation.key_claims[:5]:
                refs = ", ".join(
                    str(source_index.get(source_item_id, "?"))
                    for source_item_id in claim.source_item_ids
                )
                parts.append(f"• {html.escape(claim.text)} <i>[sources: {refs}]</i>")

        if aggregation.contradictions:
            parts.append("\n<b>Contradictions</b>")
            for contradiction in aggregation.contradictions[:3]:
                parts.append(f"• {html.escape(contradiction.summary)}")

        parts.append("\n<b>Sources</b>")
        for entry in aggregation.source_coverage[:8]:
            title = self._resolve_source_title(extraction, entry.source_item_id)
            status = "used" if entry.used_in_summary else entry.status
            parts.append(
                f"{entry.position + 1}. {html.escape(entry.source_kind.value)}"
                f" ({html.escape(status)})" + (f" — {html.escape(title)}" if title else "")
            )

        failures = [item for item in extraction.items if item.failure is not None]
        if failures:
            parts.append("\n<b>Failures</b>")
            for item in failures[:3]:
                message = item.failure.message if item.failure else "Failed"
                parts.append(f"• Source {item.position + 1}: {html.escape(message)}")

        parts.append("\n<b>Notes</b>")
        parts.append(
            f"• {aggregation.used_source_count} sources contributed to the final synthesis."
        )
        parts.append(
            "• Each key claim is traceable to one or more source items through stored provenance."
        )
        sig = aggregation.relationship_signal
        if sig is not None and sig.confidence >= 0.5 and sig.relationship_type != "unrelated":
            parts.append(
                "• Relationship signal: "
                f"{html.escape(sig.relationship_type)} "
                f"({sig.confidence:.0%} confidence)."
            )
        return "\n".join(parts)

    def _resolve_source_title(
        self,
        extraction: MultiSourceExtractionOutput,
        source_item_id: str,
    ) -> str | None:
        for item in extraction.items:
            if item.source_item_id != source_item_id:
                continue
            if item.normalized_document and item.normalized_document.title:
                return item.normalized_document.title
            return None
        return None

    async def _update_progress(
        self,
        *,
        message: Any,
        progress_message_id: int | None,
        text: str,
    ) -> None:
        if self._response_formatter.is_draft_streaming_enabled():
            await self._response_formatter.send_message_draft(message, text, force=True)
            return
        if progress_message_id is None:
            return
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if chat_id is None:
            return
        await self._response_formatter.edit_message(
            chat_id,
            progress_message_id,
            text,
            parse_mode="HTML",
        )

    async def _ensure_enabled(
        self,
        *,
        uid: int,
        message: Any,
        explicit: bool,
    ) -> bool:
        if self._rollout_gate is None:
            return True
        decision = await self._rollout_gate.evaluate(uid)
        if decision.enabled:
            return True
        if explicit:
            await self._response_formatter.safe_reply(
                message,
                decision.reason,
            )
        return False

    @staticmethod
    def _should_include_message_source(message: Any) -> bool:
        if getattr(message, "forward_from_chat", None) is not None:
            return True
        if getattr(message, "forward_from", None) is not None:
            return True
        if getattr(message, "forward_sender_name", None):
            return True
        if getattr(message, "photo", None):
            return True
        document = getattr(message, "document", None)
        if document is None:
            return False
        mime = getattr(document, "mime_type", "") or ""
        return mime.startswith("image/") or mime == "application/pdf"
