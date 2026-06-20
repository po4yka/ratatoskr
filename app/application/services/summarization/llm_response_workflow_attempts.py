"""Attempt/finalization mixin for LLM response workflow."""
# mypy: disable-error-code=attr-defined

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from app.application.services.user_interaction_update import async_safe_update_user_interaction
from app.core.call_status import CallStatus
from app.core.summary_contract_impl.quality_metadata import merge_summary_quality_metadata
from app.core.summary_normalization import normalize_metric_names
from app.domain.models.request import RequestStatus
from app.utils.json_validation import (
    finalize_summary_texts,
    parse_summary_response as _default_parse_summary_response,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from app.application.services.summarization.llm_response_workflow import AttemptContext

logger = logging.getLogger("app.application.services.summarization.llm_response_workflow")

_WORKFLOW_MODULE = "app.application.services.summarization.llm_response_workflow"


def summary_has_content(summary: dict[str, Any], required_fields: Sequence[str]) -> bool:
    """Return True if any required field contains non-empty text."""
    for field in required_fields:
        value = summary.get(field)
        if isinstance(value, str) and value.strip():
            return True
    return False


class LLMWorkflowAttemptsMixin:
    """Per-attempt processing, summary finalization, and persistence."""

    # Explicit host contract for composition with LLMResponseWorkflow.
    _attempt_json_repair: Callable[..., Any]
    _attempt_salvage_parsing: Callable[..., Any]
    _audit: Callable[..., None]
    _handle_llm_error: Callable[..., Any]
    _schedule_background_task: Callable[..., Any]
    cfg: Any
    request_repo: Any
    summary_repo: Any
    user_repo: Any

    def _get_parse_fn(self) -> Callable[..., Any]:
        """Return the parse_summary_response callable to use.

        Prefers the injected callable stored as ``_parse_summary_response``
        (set by the constructor when a ``parse_fn`` argument is supplied).
        Falls back to the module-level name on ``llm_response_workflow`` so
        that ``unittest.mock.patch("...llm_response_workflow.parse_summary_response")``
        continues to intercept calls in existing tests without requiring
        per-test constructor injection.
        """
        injected: Callable[..., Any] | None = getattr(self, "_parse_summary_response", None)
        if injected is not None:
            return injected
        import sys

        mod = sys.modules.get(_WORKFLOW_MODULE)
        if mod is not None:
            fn = getattr(mod, "parse_summary_response", None)
            if fn is not None:
                return fn
        return _default_parse_summary_response

    async def _evaluate_attempt_outcome(self, ctx: AttemptContext) -> dict[str, Any] | None:
        """Inspect the LLM result and route to salvage, error, or finalize path."""
        if ctx.llm.status != CallStatus.OK:
            salvage = None
            if (ctx.llm.error_text or "") == "structured_output_parse_error":
                salvage = self._attempt_salvage_parsing(ctx.llm, ctx.correlation_id)
            if salvage is not None:
                return await self.finalize_success(ctx, salvage)

            if ctx.is_last_attempt:
                await self._handle_llm_error(
                    ctx.message,
                    ctx.llm,
                    ctx.req_id,
                    ctx.correlation_id,
                    ctx.interaction_config,
                    ctx.notifications,
                    is_final_error=True,
                )
            else:
                await self.request_repo.async_update_request_status(ctx.req_id, RequestStatus.ERROR)
            return None

        json_parse_timeout = getattr(self.cfg.runtime, "json_parse_timeout_sec", 60.0)
        try:
            async with asyncio.timeout(json_parse_timeout):
                parse_result = await asyncio.to_thread(
                    self._get_parse_fn(),
                    ctx.llm.response_json,
                    ctx.llm.response_text,
                )
        except TimeoutError:
            logger.error(
                "json_parse_timeout",
                extra={"cid": ctx.correlation_id, "timeout_sec": json_parse_timeout},
            )
            self._set_failure_context(ctx.llm, "json_parse_timeout")
            return None
        shaped = parse_result.shaped if parse_result else None
        repair_attempted = False
        repair_succeeded = False

        if shaped is None:
            repair_attempted = True
            shaped = await self._attempt_json_repair(ctx, parse_result=parse_result)
            repair_succeeded = shaped is not None

        if shaped is None:
            self._set_failure_context(ctx.llm, "summary_parse_failed")
            return None

        finalize_summary_texts(shaped)

        if not summary_has_content(shaped, ctx.required_summary_fields):
            logger.warning(
                "summary_fields_empty",
                extra={
                    "cid": ctx.correlation_id,
                    "stage": "attempt",
                    "preset": ctx.request_config.preset_name,
                    "model": ctx.request_config.model_override,
                },
            )

            try:
                repair_hint = SimpleNamespace(errors=["missing_summary_fields"])
                repaired = await self._attempt_json_repair(ctx, parse_result=repair_hint)
                if repaired and summary_has_content(repaired, ctx.required_summary_fields):
                    return await self.finalize_success(
                        ctx,
                        repaired,
                        repair_attempted=True,
                        repair_succeeded=True,
                    )
            except Exception as exc:
                logger.warning(
                    "summary_repair_failed",
                    extra={"cid": ctx.correlation_id, "error": str(exc)},
                )

            self._set_failure_context(ctx.llm, "summary_fields_empty")
            return None

        return await self.finalize_success(
            ctx,
            shaped,
            repair_attempted=repair_attempted,
            repair_succeeded=repair_succeeded,
        )

    async def finalize_success(
        self,
        ctx: AttemptContext,
        summary: dict[str, Any],
        *,
        repair_attempted: bool = False,
        repair_succeeded: bool = False,
    ) -> dict[str, Any]:
        llm = ctx.llm
        req_id = ctx.req_id
        correlation_id = ctx.correlation_id
        interaction_config = ctx.interaction_config
        persistence = ctx.persistence
        ensure_summary = ctx.ensure_summary
        on_success = ctx.on_success
        defer_persistence = ctx.defer_persistence

        summary = normalize_metric_names(summary)

        if ensure_summary is not None:
            summary = await ensure_summary(summary)

        merge_summary_quality_metadata(
            summary,
            repair_attempted=repair_attempted,
            repair_succeeded=repair_succeeded,
            structured_output_mode=getattr(llm, "structured_output_mode", None)
            or getattr(self.cfg.openrouter, "structured_output_mode", None),
            model_used=getattr(llm, "model", None)
            or getattr(ctx.request_config, "model_override", None),
        )
        finalize_summary_texts(summary)

        if on_success is not None:
            await on_success(summary, llm)

        insights_json: dict[str, Any] | None = None
        if persistence.insights_getter is not None:
            try:
                insights_json = persistence.insights_getter(summary)
            except Exception as exc:
                logger.exception(
                    "insights_getter_failed",
                    extra={"cid": correlation_id, "error": str(exc)},
                )

        if defer_persistence or persistence.defer_write:
            self._schedule_background_task(
                self._persist_summary(
                    req_id=req_id,
                    persistence=persistence,
                    summary=summary,
                    insights_json=insights_json,
                    correlation_id=correlation_id,
                ),
                "persist_summary",
                correlation_id,
            )
        else:
            await self._persist_summary(
                req_id=req_id,
                persistence=persistence,
                summary=summary,
                insights_json=insights_json,
                correlation_id=correlation_id,
            )

        if interaction_config.interaction_id and interaction_config.success_kwargs:
            try:
                await async_safe_update_user_interaction(
                    self.user_repo,
                    interaction_id=interaction_config.interaction_id,
                    logger_=logger,
                    **interaction_config.success_kwargs,
                )
            except Exception as exc:
                logger.exception(
                    "interaction_success_update_failed",
                    extra={"cid": correlation_id, "error": str(exc)},
                )

        logger.info(
            "llm_finished_enhanced",
            extra={
                "status": llm.status,
                "latency_ms": llm.latency_ms,
                "model": llm.model,
                "cid": correlation_id,
                "summary_250_len": len(summary.get("summary_250", "")),
                "tldr_len": len(summary.get("tldr", "") or summary.get("summary_1000", "")),
                "key_ideas_count": len(summary.get("key_ideas", [])),
                "topic_tags_count": len(summary.get("topic_tags", [])),
                "entities_count": len(summary.get("entities", [])),
                "reading_time_min": summary.get("estimated_reading_time_min"),
                "seo_keywords_count": len(summary.get("seo_keywords", [])),
                "structured_output_used": getattr(llm, "structured_output_used", False),
                "structured_output_mode": getattr(llm, "structured_output_mode", None),
            },
        )

        return summary

    async def _persist_summary(
        self,
        *,
        req_id: int,
        persistence: Any,
        summary: dict[str, Any],
        insights_json: dict[str, Any] | None,
        correlation_id: str | None,
    ) -> None:
        try:
            new_version = await self.summary_repo.async_finalize_request_summary(
                request_id=req_id,
                lang=persistence.lang,
                json_payload=summary,
                insights_json=insights_json,
                is_read=persistence.is_read,
            )
            self._audit("INFO", "summary_upserted", {"request_id": req_id, "version": new_version})
        except Exception as exc:
            logger.exception(
                "persist_summary_error",
                extra={"error": str(exc), "cid": correlation_id},
            )
            raise

    def _set_failure_context(self, llm: Any, reason: str) -> None:
        """Attach a human-readable failure reason to an LLM attempt."""
        if not getattr(llm, "error_text", None):
            llm.error_text = reason

        context = getattr(llm, "error_context", None)
        if context is None:
            llm.error_context = {"message": reason}
        elif isinstance(context, dict):
            context.setdefault("message", reason)
            llm.error_context = context


__all__ = ["LLMWorkflowAttemptsMixin", "summary_has_content"]
