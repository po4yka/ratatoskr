from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.content.summarization_models import PureSummaryRequest
from app.adapters.content.url_flow_context_builder import get_url_system_prompt
from app.core.lang import choose_language, detect_language
from app.core.summary_contract_impl.quality_metadata import infer_source_coverage
from app.core.url_utils import normalize_url

from .models import StageError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class UrlBackgroundRequestHandler:
    def __init__(
        self,
        *,
        cfg: Any,
        publish_update: Callable[..., Awaitable[None]],
        run_stage: Callable[[str, str, Callable[[], Awaitable[Any]]], Awaitable[Any]],
        summary_repo_for_db: Callable[[Any], Any],
    ) -> None:
        self._cfg = cfg
        self._publish_update = publish_update
        self._run_stage = run_stage
        self._summary_repo_for_db = summary_repo_for_db

    async def process(
        self,
        *,
        request_id: int,
        request: dict[str, Any],
        db: Any,
        url_processor: Any,
        correlation_id: str,
    ) -> None:
        normalized_url = normalize_url(request.get("input_url") or "")
        await self._publish_update(
            request_id,
            "PROCESSING",
            "EXTRACTION",
            "Extracting content...",
            0.2,
            correlation_id=correlation_id,
        )
        content_text, content_source, metadata = await self._run_stage(
            "extraction",
            correlation_id,
            lambda: url_processor.content_extractor.extract_content_pure(
                url=normalized_url,
                correlation_id=correlation_id,
                request_id=request_id,
            ),
        )
        if not content_text or not content_text.strip():
            raise StageError(
                "extraction", ValueError("Content extraction failed - no content returned")
            )

        lang = self.resolve_request_language(request, content_text, metadata=metadata)
        source_coverage = infer_source_coverage(
            content_text=content_text,
            content_source=content_source,
            metadata=metadata,
        )
        system_prompt = get_url_system_prompt(lang)
        await self._publish_update(
            request_id,
            "PROCESSING",
            "SUMMARIZATION",
            "Summarizing content...",
            0.5,
            correlation_id=correlation_id,
        )
        # T9 cutover: the graph is the only summarize path. ``facade.summarize``
        # runs extraction-skipping summarize + validate + repair + enrich inside the
        # graph, so the legacy separate ``ensure_summary_payload`` validation stage
        # collapses into this single call (the VALIDATION progress update below is
        # retained for UX, the validation work now lives in the graph).
        summary_json = await self._run_stage(
            "summarization",
            correlation_id,
            lambda: url_processor.summarize(
                PureSummaryRequest(
                    content_text=content_text,
                    chosen_lang=lang,
                    system_prompt=system_prompt,
                    correlation_id=correlation_id,
                    source_coverage=source_coverage,
                    extraction_quality=metadata.get("extraction_quality")
                    if isinstance(metadata, dict)
                    else None,
                    extraction_confidence=metadata.get("extraction_confidence")
                    if isinstance(metadata, dict)
                    else None,
                )
            ),
        )
        if not summary_json:
            raise StageError(
                "summarization", ValueError("Summary generation failed - no summary returned")
            )

        await self._publish_update(
            request_id,
            "PROCESSING",
            "VALIDATION",
            "Validating summary...",
            0.8,
            correlation_id=correlation_id,
        )

        await self._publish_update(
            request_id,
            "PROCESSING",
            "SAVING",
            "Saving summary...",
            0.9,
            correlation_id=correlation_id,
        )
        repo = self._summary_repo_for_db(db)
        await repo.async_upsert_summary(
            request_id=request_id,
            lang=lang,
            json_payload=summary_json,
            is_read=False,
        )

    def resolve_request_language(
        self,
        request: dict[str, Any],
        content_text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        preferred = str(request.get("lang_detected") or "").strip().lower()
        if preferred in {"en", "ru"}:
            return preferred
        metadata_lang = ""
        if isinstance(metadata, dict):
            metadata_lang = str(metadata.get("detected_lang") or "").strip().lower()
        if metadata_lang in {"en", "ru"}:
            return choose_language(self._cfg.runtime.preferred_lang, metadata_lang)
        detected = detect_language(content_text)
        return choose_language(self._cfg.runtime.preferred_lang, detected)


class ForwardBackgroundRequestHandler:
    def __init__(
        self,
        *,
        cfg: Any,
        publish_update: Callable[..., Awaitable[None]],
        run_stage: Callable[[str, str, Callable[[], Awaitable[Any]]], Awaitable[Any]],
        summary_repo_for_db: Callable[[Any], Any],
    ) -> None:
        self._cfg = cfg
        self._publish_update = publish_update
        self._run_stage = run_stage
        self._summary_repo_for_db = summary_repo_for_db

    async def process(
        self,
        *,
        request_id: int,
        request: dict[str, Any],
        db: Any,
        url_processor: Any,
        correlation_id: str,
    ) -> None:
        lang = request.get("lang_detected") or "auto"
        if lang == "auto":
            content_text = request.get("content_text") or ""
            detected = detect_language(content_text)
            lang = choose_language(self._cfg.runtime.preferred_lang, detected)
        system_prompt = get_url_system_prompt(lang)
        await self._publish_update(
            request_id,
            "PROCESSING",
            "SUMMARIZATION",
            "Summarizing content...",
            0.5,
            correlation_id=correlation_id,
        )
        summary_json = await self._run_stage(
            "summarization",
            correlation_id,
            lambda: url_processor.summarize(
                PureSummaryRequest(
                    content_text=request.get("content_text") or "",
                    chosen_lang=lang,
                    system_prompt=system_prompt,
                    correlation_id=correlation_id,
                    source_coverage="full",
                )
            ),
        )
        if not summary_json:
            raise StageError(
                "summarization", ValueError("Summary generation failed - no summary returned")
            )

        await self._publish_update(
            request_id,
            "PROCESSING",
            "SAVING",
            "Saving summary...",
            0.9,
            correlation_id=correlation_id,
        )
        repo = self._summary_repo_for_db(db)
        await repo.async_upsert_summary(
            request_id=request_id,
            lang=lang,
            json_payload=summary_json,
            is_read=False,
        )
