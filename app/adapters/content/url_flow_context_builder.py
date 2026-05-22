"""Build prepared URL-flow context from extraction and runtime policy."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.adapters.external.formatting.single_url_progress_formatter import (
    SingleURLProgressFormatter,
)
from app.core.logging_utils import get_logger
from app.utils.progress_message_updater import ProgressMessageUpdater

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )

from app.adapters.content.url_flow_models import URLFlowContext, URLFlowRequest
from app.core.lang import LANG_RU, choose_language
from app.core.summary_contract_impl.quality_metadata import infer_source_coverage
from app.core.url_utils import compute_dedupe_hash
from app.prompts.manager import get_prompt_manager

logger = get_logger(__name__)


def get_url_system_prompt(lang: str) -> str:
    """Load the URL summarization prompt for the chosen language."""
    try:
        manager = get_prompt_manager()
        return manager.get_system_prompt(lang, include_examples=True, num_examples=2)
    except Exception as exc:
        logger.warning(
            "system_prompt_load_failed",
            extra={"lang": lang, "error": str(exc)},
        )
        return (
            "You are a precise assistant that returns only a strict JSON object "
            "matching the provided schema. Output valid UTF-8 JSON only."
        )


class URLFlowContextBuilder:
    """Prepare extraction, language, prompt, and chunking context for URL flows."""

    def __init__(
        self,
        *,
        cfg: Any,
        content_extractor: Any,
        content_chunker: Any,
        response_formatter: ResponseFormatter,
    ) -> None:
        self._cfg = cfg
        self._content_extractor = content_extractor
        self._content_chunker = content_chunker
        self._response_formatter = response_formatter

    async def build(self, request: URLFlowRequest) -> URLFlowContext:
        dedupe_hash = compute_dedupe_hash(request.url_text)
        if request.on_phase_change:
            await request.on_phase_change("extracting", None, None, None)

        # Run extraction with periodic progress updates when a tracker is available
        updater: ProgressMessageUpdater | None = None
        if request.progress_tracker is not None and not request.effective_silent:
            lang = self._cfg.runtime.preferred_lang or "en"
            updater = ProgressMessageUpdater(
                request.progress_tracker, request.message, update_interval=4.0
            )
            await updater.start(
                lambda elapsed: SingleURLProgressFormatter.format_extraction_progress(
                    url=request.url_text,
                    elapsed_sec=elapsed,
                    lang=lang,
                )
            )

        try:
            extraction = await self._content_extractor.extract_and_process_content(
                request.message,
                request.url_text,
                request.correlation_id,
                request.interaction_id,
                request.effective_silent,
                request.progress_tracker,
            )
        finally:
            # Stop periodic extraction updates without sending a final message;
            # the next pipeline notification will overwrite the progress card.
            if updater is not None:
                updater._stop_event.set()
                if updater._task is not None:
                    updater._task.cancel()
                    await asyncio.gather(updater._task, return_exceptions=True)
                    updater._task = None
        req_id = extraction.request_id
        content_text = extraction.content_text
        content_source = extraction.content_source
        detected = extraction.detected_lang
        title = extraction.title
        images = extraction.images
        source_coverage = infer_source_coverage(
            content_text=content_text,
            content_source=content_source,
        )

        if getattr(self._cfg.runtime, "url_flow_streaming_enabled", True):
            from app.adapters.content.streaming import StreamEvent, get_stream_hub

            get_stream_hub().publish(
                str(req_id),
                StreamEvent.now("stage", {"stage": "extracting"}, request.correlation_id or ""),
            )

        chosen_lang = choose_language(self._cfg.runtime.preferred_lang, detected)
        needs_ru_translation = not request.silent and LANG_RU not in (detected, chosen_lang)
        system_prompt = get_url_system_prompt(chosen_lang)

        logger.debug(
            "language_choice",
            extra={"detected": detected, "chosen": chosen_lang, "cid": request.correlation_id},
        )
        if not request.silent and not request.batch_mode:
            content_preview = (
                content_text[:150] + "..." if len(content_text) > 150 else content_text
            )
            await self._response_formatter.send_language_detection_notification(
                request.message,
                detected,
                content_preview,
                url=request.url_text,
                silent=request.silent,
            )

        should_chunk, max_chars, chunks = self._compute_chunk_strategy(
            content_text=content_text,
            chosen_lang=chosen_lang,
            correlation_id=request.correlation_id,
        )
        if not request.batch_mode:
            await self._response_formatter.send_content_analysis_notification(
                request.message,
                len(content_text),
                max_chars,
                should_chunk,
                chunks,
                self._cfg.openrouter.structured_output_mode,
                silent=request.silent,
            )

        return URLFlowContext(
            dedupe_hash=dedupe_hash,
            req_id=req_id,
            content_text=content_text,
            title=title,
            images=images,
            chosen_lang=chosen_lang,
            needs_ru_translation=needs_ru_translation,
            system_prompt=system_prompt,
            should_chunk=should_chunk,
            max_chars=max_chars,
            chunks=chunks,
            source_coverage=source_coverage,
        )

    def _compute_chunk_strategy(
        self,
        *,
        content_text: str,
        chosen_lang: str,
        correlation_id: str | None,
    ) -> tuple[bool, int, list[str] | None]:
        should_chunk, max_chars, chunks = self._content_chunker.should_chunk_content(
            content_text,
            chosen_lang,
        )
        long_context_model = self._cfg.openrouter.long_context_model
        if should_chunk and long_context_model:
            logger.info(
                "chunking_bypassed_long_context",
                extra={
                    "cid": correlation_id,
                    "long_context_model": long_context_model,
                    "content_length": len(content_text),
                },
            )
            should_chunk = False
            chunks = None

        logger.info(
            "content_handling",
            extra={
                "cid": correlation_id,
                "length": len(content_text),
                "should_chunk": should_chunk,
                "chunks": len(chunks) if chunks else 0,
            },
        )
        return should_chunk, max_chars, chunks
