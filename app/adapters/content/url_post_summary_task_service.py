"""Post-summary follow-up tasks for URL flows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    import asyncio

    from app.adapters.content.llm_summarizer_articles import LLMArticleGenerator
    from app.adapters.content.llm_summarizer_insights import LLMInsightsGenerator
    from app.adapters.content.url_summary_delivery_service import URLSummaryDeliveryService
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.application.ports.summaries import SummaryRepositoryPort
    from app.application.services.related_reads_service import RelatedReadsService

from app.core.async_utils import raise_if_cancelled

logger = get_logger(__name__)

# Header that introduces the full Russian rendering of a summary (always Russian,
# regardless of the configured UI language).
_RU_SUMMARY_HEADER = "🇷🇺 Версия на русском"


class URLPostSummaryTaskService:
    """Own translation, insights, custom article, and related-reads follow-up work."""

    def __init__(
        self,
        *,
        response_formatter: ResponseFormatter,
        summary_repo: SummaryRepositoryPort,
        article_generator: LLMArticleGenerator,
        insights_generator: LLMInsightsGenerator,
        summary_delivery: URLSummaryDeliveryService,
        related_reads_service: RelatedReadsService | None = None,
        cfg: Any = None,
        llm_client: Any = None,
    ) -> None:
        self._response_formatter = response_formatter
        self._summary_repo = summary_repo
        self._article_generator = article_generator
        self._insights_generator = insights_generator
        self._summary_delivery = summary_delivery
        self._related_reads_service = related_reads_service
        self._cfg = cfg
        self._llm_client = llm_client
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _bilingual_enabled(self) -> bool:
        return bool(
            getattr(getattr(self._cfg, "runtime", None), "summary_bilingual_enabled", False)
        )

    def set_related_reads_service(self, service: RelatedReadsService | None) -> None:
        """Inject or replace the related-reads service after construction."""
        self._related_reads_service = service

    async def aclose(self, timeout: float = 5.0) -> None:
        """Drain outstanding post-summary background tasks."""
        await self._summary_delivery.drain_tasks(
            self._background_tasks,
            timeout=timeout,
            timeout_event="url_post_summary_shutdown_timeout",
            complete_event="url_post_summary_shutdown_complete",
        )

    async def schedule_tasks(
        self,
        message: Any,
        content_text: str,
        chosen_lang: str,
        req_id: int,
        correlation_id: str | None,
        summary: dict[str, Any],
        *,
        needs_ru_translation: bool,
        silent: bool,
        url_hash: str | None,
    ) -> None:
        # Bilingual delivery: for non-Russian content deliver the WHOLE summary in
        # Russian (every field), not just the TL;DR. Falls back to the legacy prose
        # translation when bilingual mode is off or the structured pass fails.
        _has_inline_ru = bool(str(summary.get("tldr_ru") or "").strip())
        if needs_ru_translation and not silent and self._bilingual_enabled():
            # Awaited inline (not fire-and-forget) so the Russian block lands right
            # after the primary-language summary, before the insights/article
            # follow-ups. The primary summary is already delivered at this point,
            # so this only delays the secondary enrichment notices, not the summary.
            ru_sent = await self._send_bilingual_ru_summary(
                message, summary, req_id, correlation_id
            )
            if not ru_sent and not _has_inline_ru:
                self._schedule_task(
                    self._maybe_send_russian_translation(
                        message,
                        summary,
                        req_id,
                        correlation_id,
                        needs_ru_translation,
                        url_hash=url_hash,
                        source_lang=chosen_lang,
                    ),
                    correlation_id,
                    "ru_translation",
                )
        elif needs_ru_translation and not _has_inline_ru:
            self._schedule_task(
                self._maybe_send_russian_translation(
                    message,
                    summary,
                    req_id,
                    correlation_id,
                    needs_ru_translation,
                    url_hash=url_hash,
                    source_lang=chosen_lang,
                ),
                correlation_id,
                "ru_translation",
            )

        reader_mode = False
        if not silent:
            try:
                reader_mode = await self._response_formatter.is_reader_mode(message)
            except Exception:
                reader_mode = False

        if not silent and not reader_mode:
            try:
                await self._response_formatter.safe_reply(
                    message,
                    "🧠 Generating additional research insights…",
                )
            except Exception as exc:
                raise_if_cancelled(exc)

        self._schedule_task(
            self._handle_additional_insights(
                message,
                content_text,
                chosen_lang,
                req_id,
                correlation_id,
                summary=summary,
                silent=silent,
                url_hash=url_hash,
            ),
            correlation_id,
            "additional_insights",
        )

        if not silent:
            topics = summary.get("key_ideas") or []
            tags = summary.get("topic_tags") or []
            if (topics or tags) and isinstance(topics, list) and isinstance(tags, list):
                if not reader_mode:
                    try:
                        await self._response_formatter.safe_reply(
                            message,
                            "📝 Crafting a standalone article from topics & tags…",
                        )
                    except Exception as exc:
                        raise_if_cancelled(exc)

                if not reader_mode:
                    self._schedule_task(
                        self._handle_custom_article(
                            message,
                            chosen_lang,
                            req_id,
                            correlation_id,
                            topics,
                            tags,
                            url_hash=url_hash,
                        ),
                        correlation_id,
                        "custom_article",
                    )

        if self._related_reads_service is not None and not silent:
            self._schedule_task(
                self._run_related_reads(
                    message,
                    summary_payload=summary,
                    request_id=req_id,
                    correlation_id=correlation_id,
                    lang=chosen_lang,
                ),
                correlation_id,
                "related_reads",
            )

    async def translate_summary_to_ru(
        self,
        summary: dict[str, Any],
        *,
        req_id: int,
        correlation_id: str | None = None,
        url_hash: str | None = None,
        source_lang: str | None = None,
    ) -> str | None:
        return await self._article_generator.translate_summary_to_ru(
            summary,
            req_id=req_id,
            correlation_id=correlation_id,
            url_hash=url_hash,
            source_lang=source_lang,
        )

    async def _send_bilingual_ru_summary(
        self,
        message: Any,
        summary: dict[str, Any],
        req_id: int,
        correlation_id: str | None,
    ) -> bool:
        """Translate the finished summary and deliver the full Russian version.

        Returns True when the Russian block was delivered; False when translation
        or delivery did not happen, so the caller can fall back to the legacy
        prose translation.
        """
        if self._llm_client is None:
            return False
        from app.adapters.content.summary_translation import translate_summary_to_ru_struct

        try:
            ru_summary = await translate_summary_to_ru_struct(
                llm_client=self._llm_client,
                summary=summary,
                cfg=self._cfg,
                correlation_id=correlation_id,
                req_id=req_id,
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "bilingual_ru_translation_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )
            return False

        if not ru_summary:
            return False

        # Persist the Russian summary alongside the primary one so it is not
        # ephemeral (available to exports / API / web). Best-effort: a DB failure
        # must not block Telegram delivery.
        try:
            await self._summary_repo.async_update_ru_payload(req_id, ru_summary)
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "bilingual_ru_persist_failed",
                extra={"cid": correlation_id, "req_id": req_id, "error": str(exc)},
            )

        try:
            return await self._response_formatter.send_secondary_language_summary(
                message,
                ru_summary,
                lang="ru",
                header=_RU_SUMMARY_HEADER,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "bilingual_ru_delivery_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )
            return False

    async def _maybe_send_russian_translation(
        self,
        message: Any,
        summary: dict[str, Any],
        req_id: int,
        correlation_id: str | None,
        needs_translation: bool,
        *,
        url_hash: str | None = None,
        source_lang: str | None = None,
    ) -> None:
        if not needs_translation:
            return

        try:
            translated = await self.translate_summary_to_ru(
                summary,
                req_id=req_id,
                correlation_id=correlation_id,
                url_hash=url_hash,
                source_lang=source_lang,
            )
            if translated:
                await self._response_formatter.send_russian_translation(
                    message,
                    translated,
                    correlation_id=correlation_id,
                )
                return

            await self._response_formatter.safe_reply(
                message,
                (
                    "⚠️ Unable to generate Russian translation right now. Error ID: "
                    f"{correlation_id or 'unknown'}."
                ),
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.exception(
                "ru_translation_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )
            try:
                await self._response_formatter.safe_reply(
                    message,
                    f"⚠️ Russian translation failed. Error ID: {correlation_id or 'unknown'}.",
                )
            except Exception as reply_exc:
                raise_if_cancelled(reply_exc)

    async def _handle_additional_insights(
        self,
        message: Any,
        content_text: str,
        chosen_lang: str,
        req_id: int,
        correlation_id: str | None,
        *,
        summary: dict[str, Any] | None = None,
        silent: bool = False,
        url_hash: str | None = None,
    ) -> None:
        logger.info(
            "insights_flow_started",
            extra={"cid": correlation_id, "content_len": len(content_text), "lang": chosen_lang},
        )

        try:
            insights = await self._insights_generator.generate_additional_insights(
                message,
                content_text=content_text,
                chosen_lang=chosen_lang,
                req_id=req_id,
                correlation_id=correlation_id,
                summary=summary,
                url_hash=url_hash,
            )
            if not insights:
                logger.warning(
                    "insights_generation_returned_empty",
                    extra={"cid": correlation_id, "reason": "LLM returned None or empty insights"},
                )
                return

            logger.info(
                "insights_generated_successfully",
                extra={
                    "cid": correlation_id,
                    "facts_count": len(insights.get("new_facts", [])),
                    "has_overview": bool(insights.get("topic_overview")),
                },
            )

            should_notify = not silent
            if should_notify:
                try:
                    should_notify = not (await self._response_formatter.is_reader_mode(message))
                except Exception:
                    should_notify = True

            if should_notify:
                await self._response_formatter.send_additional_insights_message(
                    message,
                    insights,
                    correlation_id,
                )
                logger.info("insights_message_sent", extra={"cid": correlation_id})
            else:
                logger.info(
                    "insights_notification_skipped",
                    extra={"cid": correlation_id, "reason": "reader_mode_or_silent"},
                )

            try:
                await self._summary_repo.async_update_summary_insights(req_id, insights)
                logger.debug(
                    "insights_persisted",
                    extra={"cid": correlation_id, "request_id": req_id},
                )
            except Exception as exc:
                raise_if_cancelled(exc)
                logger.error(
                    "persist_insights_error",
                    extra={"cid": correlation_id, "error": str(exc)},
                )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.exception(
                "insights_flow_error",
                extra={"cid": correlation_id, "error": str(exc)},
            )

    async def _handle_custom_article(
        self,
        message: Any,
        chosen_lang: str,
        req_id: int,
        correlation_id: str | None,
        topics: list[Any],
        tags: list[Any],
        *,
        url_hash: str | None = None,
    ) -> None:
        try:
            article = await self._article_generator.generate_custom_article(
                message,
                chosen_lang=chosen_lang,
                req_id=req_id,
                topics=[str(x) for x in topics if str(x).strip()],
                tags=[str(x) for x in tags if str(x).strip()],
                correlation_id=correlation_id,
                url_hash=url_hash,
            )
            if article:
                await self._response_formatter.send_custom_article(message, article)
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.error(
                "custom_article_flow_error",
                extra={"cid": correlation_id, "error": str(exc)},
            )

    async def _run_related_reads(
        self,
        message: Any,
        *,
        summary_payload: dict[str, Any],
        request_id: int,
        correlation_id: str | None,
        lang: str,
    ) -> None:
        try:
            items = await self._related_reads_service.find_related(
                summary_payload,
                exclude_request_id=request_id,
            )
            if items:
                await self._response_formatter.send_related_reads(
                    message,
                    items,
                    lang=lang,
                )
        except Exception as exc:
            logger.warning(
                "related_reads_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )

    def _schedule_task(
        self,
        coro: Any,
        correlation_id: str | None,
        label: str,
    ) -> None:
        self._summary_delivery.schedule_task(
            self._background_tasks,
            coro,
            correlation_id,
            label,
            schedule_error_event="background_task_schedule_failed",
            task_error_event="background_task_failed",
        )
