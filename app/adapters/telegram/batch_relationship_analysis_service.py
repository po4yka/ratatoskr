"""Relationship analysis orchestration for completed URL batches."""

from __future__ import annotations

import html
import json
import time
from functools import partial
from typing import TYPE_CHECKING, Any, TypedDict, cast
from urllib.parse import urlparse

from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.config.integrations import BatchAnalysisConfig

from app.adapters.telegram.batch_sender_utils import (
    is_draft_streaming_enabled as _is_draft_streaming_enabled,
    resolve_sender as _resolve_sender,
    send_message_draft_safe as _send_message_draft_safe,
)

logger = get_logger(__name__)


class _SummaryPayload(TypedDict, total=False):
    title: str | None
    author: str | None
    published_at: str | None
    topic_tags: list[str]
    entities: list[Any]
    summary_250: str | None
    summary_1000: str | None


class BatchRelationshipAnalysisService:
    """Runs relationship analysis and combined-summary generation for batches."""

    def __init__(
        self,
        *,
        summary_repo: Any | None,
        batch_session_repo: Any | None,
        llm_client: Any | None,
        batch_config: BatchAnalysisConfig | None,
        response_formatter: ResponseFormatter | None,
    ) -> None:
        self._summary_repo = summary_repo
        self._batch_session_repo = batch_session_repo
        self._llm_client = llm_client
        self._batch_config = batch_config
        self._response_formatter = response_formatter

    @property
    def is_configured(self) -> bool:
        return all(
            value is not None
            for value in (
                self._summary_repo,
                self._batch_session_repo,
                self._llm_client,
                self._batch_config,
                self._response_formatter,
            )
        )

    async def analyze_batch(self, *, batch_result: Any, message: Any) -> None:
        """Analyze a completed batch and send a relationship summary when relevant."""
        if not self.is_configured:
            return

        from app.adapter_models.batch_analysis import RelationshipType

        batch_status = batch_result.batch_status
        correlation_id = batch_result.correlation_id
        if batch_status.success_count < self._batch_config.min_articles:
            logger.debug(
                "batch_analysis_skipped_insufficient_articles",
                extra={
                    "success_count": batch_status.success_count,
                    "min_required": self._batch_config.min_articles,
                    "cid": correlation_id,
                },
            )
            return

        start_time_ms = time.time() * 1000
        sender = _resolve_sender(self._response_formatter)
        draft_enabled = _is_draft_streaming_enabled(sender)
        preview_buffer = [""]
        on_stream_delta = partial(
            self._combined_stream_delta,
            sender,
            message,
            draft_enabled,
            preview_buffer,
        )

        session_id: int | None = None
        try:
            await self._draft_stage_update(
                sender,
                message,
                draft_enabled,
                "🔗 Batch analysis: preparing article relationships...",
            )
            prepared = await self._prepare_batch_analysis_inputs(
                batch_status=batch_status,
                url_to_request_id=batch_result.url_to_request_id,
                uid=batch_result.uid,
                correlation_id=correlation_id,
            )
            if prepared is None:
                return

            session_id, articles, full_summaries, language = prepared
            await self._draft_stage_update(
                sender,
                message,
                draft_enabled,
                "🧠 Batch analysis: running relationship detection...",
            )
            relationship = await self._run_relationship_analysis(
                session_id=session_id,
                correlation_id=correlation_id,
                articles=articles,
                language=language,
            )
            if relationship is None:
                return

            await self._batch_session_repo.async_update_batch_session_relationship(
                session_id,
                relationship_type=relationship.relationship_type.value,
                relationship_confidence=relationship.confidence,
                relationship_metadata=self._build_relationship_metadata(relationship),
            )

            if relationship.relationship_type == RelationshipType.UNRELATED:
                await self._complete_unrelated_batch(
                    session_id=session_id,
                    start_time_ms=start_time_ms,
                    correlation_id=correlation_id,
                )
                return

            if relationship.series_info and relationship.series_info.article_order:
                await self._persist_series_item_order(
                    session_id=session_id,
                    article_order=relationship.series_info.article_order,
                    series_title=relationship.series_info.series_title,
                )

            await self._draft_stage_update(
                sender,
                message,
                draft_enabled,
                "🧩 Batch analysis: generating combined summary...",
            )
            combined_summary = await self._maybe_generate_combined_summary(
                correlation_id=correlation_id,
                articles=articles,
                relationship=relationship,
                full_summaries=full_summaries,
                language=language,
                stream=draft_enabled,
                on_stream_delta=on_stream_delta if draft_enabled else None,
            )
            if combined_summary is not None:
                await self._batch_session_repo.async_update_batch_session_combined_summary(
                    session_id,
                    combined_summary.model_dump(),
                )

            processing_time_ms = int(time.time() * 1000 - start_time_ms)
            await self._batch_session_repo.async_update_batch_session_status(
                session_id,
                "completed",
                processing_time_ms=processing_time_ms,
            )
            await self._send_batch_analysis_result(
                message=message,
                relationship=relationship,
                combined_summary=combined_summary,
                articles=articles,
                language=language,
            )
            if draft_enabled:
                self._clear_message_draft(sender, message)

            logger.info(
                "batch_analysis_complete",
                extra={
                    "session_id": session_id,
                    "relationship_type": relationship.relationship_type.value,
                    "confidence": relationship.confidence,
                    "combined_summary": combined_summary is not None,
                    "processing_time_ms": processing_time_ms,
                    "cid": correlation_id,
                },
            )
        except Exception as exc:
            logger.exception(
                "batch_relationship_analysis_error",
                extra={"error": str(exc), "cid": correlation_id},
            )
            if session_id is not None:
                await self._batch_session_repo.async_update_batch_session_status(
                    session_id, "failed"
                )
        finally:
            if draft_enabled:
                self._clear_message_draft(sender, message)

    async def _draft_stage_update(
        self,
        sender: Any,
        message: Any,
        draft_enabled: bool,
        text: str,
    ) -> None:
        if not draft_enabled:
            return
        await _send_message_draft_safe(sender, message, text, force=True)

    async def _combined_stream_delta(
        self,
        sender: Any,
        message: Any,
        draft_enabled: bool,
        preview_buffer: list[str],
        delta: str,
    ) -> None:
        if not draft_enabled or not delta:
            return
        preview_buffer[0] += delta
        preview = preview_buffer[0][-1400:].strip()
        if not preview:
            return
        await _send_message_draft_safe(
            sender,
            message,
            f"🔗 Relationship detected. Building combined summary...\n\n{preview}",
        )

    def _clear_message_draft(self, sender: Any, message: Any) -> None:
        clear_draft = getattr(sender, "clear_message_draft", None)
        if callable(clear_draft):
            clear_draft(message)

    async def _prepare_batch_analysis_inputs(
        self,
        *,
        batch_status: Any,
        url_to_request_id: dict[str, int],
        uid: int,
        correlation_id: str,
    ) -> tuple[int, list[Any], list[dict[str, Any]], str] | None:
        session_id = await self._batch_session_repo.async_create_batch_session(
            user_id=uid,
            correlation_id=correlation_id,
            total_urls=batch_status.total,
        )
        successful_request_ids = self._collect_successful_request_ids(
            batch_status,
            url_to_request_id,
        )
        if len(successful_request_ids) < self._batch_config.min_articles:
            await self._batch_session_repo.async_update_batch_session_status(
                session_id,
                "completed",
                analysis_status="skipped",
            )
            return None

        summaries = await self._summary_repo.async_get_summaries_by_request_ids(
            successful_request_ids
        )
        articles, full_summaries = await self._build_articles_for_analysis(
            session_id=session_id,
            successful_request_ids=successful_request_ids,
            summaries=summaries,
            url_to_request_id=url_to_request_id,
        )
        if len(articles) < self._batch_config.min_articles:
            await self._batch_session_repo.async_update_batch_session_status(
                session_id,
                "completed",
                analysis_status="skipped",
            )
            return None

        await self._batch_session_repo.async_update_batch_session_counts(
            session_id,
            successful_count=batch_status.success_count,
            failed_count=batch_status.fail_count,
        )
        languages = [article.language for article in articles if article.language]
        language = languages[0] if languages else "en"
        return session_id, articles, full_summaries, language

    def _collect_successful_request_ids(
        self,
        batch_status: Any,
        url_to_request_id: dict[str, int],
    ) -> list[int]:
        from app.adapter_models.batch_processing import URLStatus

        successful_request_ids: list[int] = []
        for url, request_id in url_to_request_id.items():
            entry = batch_status._find_entry(url)
            if entry and entry.status in (URLStatus.COMPLETE, URLStatus.CACHED):
                successful_request_ids.append(request_id)
        return successful_request_ids

    async def _build_articles_for_analysis(
        self,
        *,
        session_id: int,
        successful_request_ids: list[int],
        summaries: dict[int, dict[str, Any]],
        url_to_request_id: dict[str, int],
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        from app.adapter_models.batch_analysis import ArticleMetadata

        articles: list[Any] = []
        full_summaries: list[dict[str, Any]] = []
        for index, request_id in enumerate(successful_request_ids):
            summary_data = summaries.get(request_id)
            if not summary_data:
                continue

            await self._batch_session_repo.async_add_batch_session_item(
                session_id=session_id,
                request_id=request_id,
                position=index,
            )
            payload = self._payload_to_dict(summary_data)
            url = next(
                (item_url for item_url, rid in url_to_request_id.items() if rid == request_id), ""
            )
            domain = ""
            if url:
                try:
                    domain = urlparse(url).netloc
                except (ValueError, AttributeError):
                    logger.debug("domain_parse_failed", extra={"url": url})

            articles.append(
                ArticleMetadata(
                    request_id=request_id,
                    url=url,
                    title=payload.get("title"),
                    author=payload.get("author"),
                    domain=domain,
                    published_at=payload.get("published_at"),
                    topic_tags=payload.get("topic_tags", []),
                    entities=self._extract_entity_names(payload.get("entities", [])),
                    summary_250=payload.get("summary_250"),
                    summary_1000=payload.get("summary_1000"),
                    language=summary_data.get("lang"),
                )
            )
            full_summaries.append(cast("dict[str, Any]", payload))
        return articles, full_summaries

    def _payload_to_dict(self, summary_data: dict[str, Any]) -> _SummaryPayload:
        raw_payload = summary_data.get("json_payload", {})
        if isinstance(raw_payload, str):
            try:
                raw_payload = json.loads(raw_payload)
            except (json.JSONDecodeError, ValueError):
                raw_payload = {}
        if isinstance(raw_payload, dict):
            return cast("_SummaryPayload", raw_payload)
        return cast("_SummaryPayload", {})

    def _extract_entity_names(self, raw_entities: Any) -> list[str]:
        entities: list[str] = []
        if isinstance(raw_entities, list):
            for entity in raw_entities:
                if isinstance(entity, dict) and "name" in entity:
                    entities.append(entity["name"])
                elif isinstance(entity, str):
                    entities.append(entity)
        return entities

    async def _run_relationship_analysis(
        self,
        *,
        session_id: int,
        correlation_id: str,
        articles: list[Any],
        language: str,
    ) -> Any | None:
        from app.adapter_models.batch_analysis import RelationshipAnalysisInput
        from app.agents.relationship_analysis_agent import RelationshipAnalysisAgent

        await self._batch_session_repo.async_update_batch_session_status(
            session_id,
            "processing",
            analysis_status="analyzing",
        )
        relationship_agent = RelationshipAnalysisAgent(
            llm_client=self._llm_client if self._batch_config.use_llm_for_analysis else None,
            correlation_id=correlation_id,
        )
        analysis_input = RelationshipAnalysisInput(
            articles=articles,
            correlation_id=correlation_id,
            language=language,
            series_threshold=self._batch_config.series_threshold,
            cluster_threshold=self._batch_config.cluster_threshold,
        )
        analysis_result = await relationship_agent.execute(analysis_input)
        if not analysis_result.success or not analysis_result.output:
            logger.warning(
                "batch_relationship_analysis_failed",
                extra={"error": analysis_result.error, "cid": correlation_id},
            )
            await self._batch_session_repo.async_update_batch_session_status(
                session_id,
                "completed",
                analysis_status=CallStatus.ERROR,
            )
            return None
        return analysis_result.output

    def _build_relationship_metadata(self, relationship: Any) -> dict[str, Any]:
        return {
            "series_info": relationship.series_info.model_dump()
            if relationship.series_info
            else None,
            "cluster_info": relationship.cluster_info.model_dump()
            if relationship.cluster_info
            else None,
            "reasoning": relationship.reasoning,
            "signals_used": relationship.signals_used,
        }

    async def _complete_unrelated_batch(
        self,
        *,
        session_id: int,
        start_time_ms: float,
        correlation_id: str,
    ) -> None:
        processing_time_ms = int(time.time() * 1000 - start_time_ms)
        await self._batch_session_repo.async_update_batch_session_status(
            session_id,
            "completed",
            processing_time_ms=processing_time_ms,
        )
        logger.info(
            "batch_analysis_complete_unrelated",
            extra={"session_id": session_id, "cid": correlation_id},
        )

    async def _persist_series_item_order(
        self,
        *,
        session_id: int,
        article_order: list[int],
        series_title: str | None,
    ) -> None:
        items = await self._batch_session_repo.async_get_batch_session_items(session_id)
        for index, request_id in enumerate(article_order, 1):
            for item in items:
                if item.get("request") == request_id:
                    await self._batch_session_repo.async_update_batch_session_item_series_info(
                        item["id"],
                        is_series_part=True,
                        series_order=index,
                        series_title=series_title,
                    )
                    break

    async def _maybe_generate_combined_summary(
        self,
        *,
        correlation_id: str,
        articles: list[Any],
        relationship: Any,
        full_summaries: list[dict[str, Any]],
        language: str,
        stream: bool = False,
        on_stream_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> Any | None:
        if not (self._batch_config.combined_summary_enabled and self._llm_client):
            return None

        from app.adapter_models.batch_analysis import CombinedSummaryInput
        from app.agents.combined_summary_agent import CombinedSummaryAgent

        combined_agent = CombinedSummaryAgent(
            llm_client=self._llm_client,
            correlation_id=correlation_id,
            stream=stream,
            on_stream_delta=on_stream_delta,
        )
        combined_input = CombinedSummaryInput(
            articles=articles,
            relationship=relationship,
            full_summaries=full_summaries,
            correlation_id=correlation_id,
            language=language,
        )
        combined_result = await combined_agent.execute(combined_input)
        if combined_result.success and combined_result.output:
            return combined_result.output
        return None

    async def _send_batch_analysis_result(
        self,
        *,
        message: Any,
        relationship: Any,
        combined_summary: Any,
        articles: list[Any],
        language: str,
    ) -> None:
        from app.adapter_models.batch_analysis import RelationshipType

        _ = articles
        type_labels = {
            RelationshipType.SERIES: ("Series Detected", "Обнаружена серия"),
            RelationshipType.TOPIC_CLUSTER: ("Topic Cluster", "Тематический кластер"),
            RelationshipType.AUTHOR_COLLECTION: ("Author Collection", "Коллекция автора"),
            RelationshipType.DOMAIN_RELATED: ("Related Content", "Связанный контент"),
        }

        type_label = type_labels.get(relationship.relationship_type, ("Related", "Связано"))
        label = type_label[1] if language == "ru" else type_label[0]

        # Sent with parse_mode="HTML" below; every value below is LLM-derived
        # from summaries of untrusted third-party articles, so html.escape() each
        # one to stop a crafted topic/entity/arc from injecting Telegram markup
        # into the bot's own output. Labels/headers here are static and trusted.
        parts = [f"<b>{label}</b> ({relationship.confidence:.0%} confidence)"]
        if relationship.reasoning:
            parts.append(f"\n{html.escape(str(relationship.reasoning))}")

        if relationship.series_info:
            series_info = relationship.series_info
            if series_info.series_title:
                parts.append(f"\n<b>Series:</b> {html.escape(str(series_info.series_title))}")
            if series_info.numbering_pattern:
                parts.append(f"<b>Pattern:</b> {html.escape(str(series_info.numbering_pattern))}")

        if relationship.cluster_info:
            cluster_info = relationship.cluster_info
            if cluster_info.cluster_topic:
                parts.append(f"\n<b>Topic:</b> {html.escape(str(cluster_info.cluster_topic))}")
            if cluster_info.shared_entities:
                entities = ", ".join(html.escape(str(e)) for e in cluster_info.shared_entities[:5])
                parts.append(f"<b>Shared entities:</b> {entities}")
            if cluster_info.shared_tags:
                tags = ", ".join(html.escape(str(tg)) for tg in cluster_info.shared_tags[:5])
                parts.append(f"<b>Shared tags:</b> {tags}")

        if combined_summary:
            parts.append("\n---")
            parts.append(
                f"\n<b>Thematic Arc:</b>\n{html.escape(str(combined_summary.thematic_arc))}"
            )

            if combined_summary.synthesized_insights:
                insights_header = (
                    "Synthesized Insights" if language != "ru" else "Синтезированные инсайты"
                )
                parts.append(f"\n<b>{insights_header}:</b>")
                for insight in combined_summary.synthesized_insights[:5]:
                    parts.append(f"- {html.escape(str(insight))}")

            if combined_summary.contradictions:
                contradictions_header = "Contradictions" if language != "ru" else "Противоречия"
                parts.append(f"\n<b>{contradictions_header}:</b>")
                for contradiction in combined_summary.contradictions[:3]:
                    parts.append(f"- {html.escape(str(contradiction))}")

            if combined_summary.reading_order_rationale:
                order_header = "Reading Order" if language != "ru" else "Порядок чтения"
                rationale = html.escape(str(combined_summary.reading_order_rationale))
                parts.append(f"\n<b>{order_header}:</b> {rationale}")

            if combined_summary.total_reading_time_min:
                time_header = "Total Reading Time" if language != "ru" else "Общее время чтения"
                parts.append(
                    f"\n<b>{time_header}:</b> {combined_summary.total_reading_time_min} min"
                )

        await self._response_formatter.safe_reply(message, "\n".join(parts), parse_mode="HTML")
