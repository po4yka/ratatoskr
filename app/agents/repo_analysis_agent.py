"""Repo analysis agent with self-correction feedback loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from pydantic import ValidationError

from app.agents.llm_call_persistence import persist_agent_llm_call
from app.core.content_cleaner import wrap_untrusted_source
from app.core.logging_utils import get_logger
from app.core.repo_analysis_contract import parse_and_validate_repo_analysis
from app.observability.attributes import AGENT_ATTEMPT, AGENT_NAME, REQUEST_CORRELATION_ID
from app.prompts.file_cache import read_prompt_text

if TYPE_CHECKING:
    from pydantic import BaseModel

    from app.adapter_models.llm.llm_models import StructuredLLMResult
    from app.application.ports.requests import LLMRepositoryPort
    from app.core.repo_analysis_schema import RepoAnalysis, RepoAnalysisInput

logger = get_logger(__name__)


# Lazy import to avoid early-binding the OTel TracerProvider at module import
# time, which can interfere with test-level provider setup.
def _get_tracer() -> Any:
    from app.observability.otel import get_tracer

    return get_tracer(__name__)


_PROMPT_DIR = Path(__file__).parent.parent / "prompts"


@runtime_checkable
class LLMServiceProtocol(Protocol):
    """Minimal LLM interface required by RepoAnalysisAgent."""

    async def call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        correlation_id: str,
    ) -> str:
        """Return the raw text response from the LLM."""
        ...


@runtime_checkable
class StructuredLLMServiceProtocol(Protocol):
    """Structured-output LLM interface preferred by RepoAnalysisAgent."""

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        *,
        response_model: type[BaseModel],
        max_retries: int = 3,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        request_id: int | None = None,
        model_override: str | None = None,
        fallback_models_override: tuple[str, ...] | list[str] | None = None,
    ) -> StructuredLLMResult[Any]:
        """Return a Pydantic-validated structured LLM response."""
        ...


class RepoAnalysisAgent:
    """Analyse a GitHub repository via LLM with structured-output validation.

    The agent:
    - Loads the correct system prompt for the chosen language.
    - Serialises ``RepoAnalysisInput`` as JSON and sends it to the LLM.
    - Prefers provider-backed ``chat_structured`` validation against ``RepoAnalysis``.
    - Falls back to the legacy raw-text repair loop for older test doubles/adapters.
    - Persists one ``LLMCall``-shaped record per attempt via ``llm_repo``
      (optional; skipped when not provided).
    """

    def __init__(
        self,
        llm_service: LLMServiceProtocol | StructuredLLMServiceProtocol,
        llm_repo: LLMRepositoryPort | None = None,
        request_id: int | None = None,
        model_name: str | None = None,
    ) -> None:
        self._llm = llm_service
        self._llm_repo = llm_repo
        self._request_id = request_id
        self._model_name = model_name

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze(
        self,
        input: RepoAnalysisInput,
        *,
        chosen_lang: Literal["en", "ru"] = "en",
        correlation_id: str,
        max_attempts: int = 3,
    ) -> RepoAnalysis | None:
        """Run LLM analysis with a retry-on-validation-error loop.

        Returns a ``RepoAnalysis`` on success, or ``None`` after
        ``max_attempts`` consecutive failures.
        """
        with _get_tracer().start_as_current_span("agent.repo_analysis") as span:
            span.set_attribute(AGENT_NAME, "repo_analysis")
            span.set_attribute(REQUEST_CORRELATION_ID, correlation_id)
            span.set_attribute(AGENT_ATTEMPT, 1)
            if isinstance(self._llm, StructuredLLMServiceProtocol):
                return await self._analyze_structured(
                    input,
                    chosen_lang=chosen_lang,
                    correlation_id=correlation_id,
                    max_attempts=max_attempts,
                )
            return await self._analyze_legacy(
                input,
                chosen_lang=chosen_lang,
                correlation_id=correlation_id,
                max_attempts=max_attempts,
            )

    async def _analyze_structured(
        self,
        input: RepoAnalysisInput,
        *,
        chosen_lang: Literal["en", "ru"],
        correlation_id: str,
        max_attempts: int,
    ) -> RepoAnalysis | None:
        """Run repository analysis through provider-native structured output."""
        from app.core.repo_analysis_schema import RepoAnalysis

        system_prompt = self._load_system_prompt(chosen_lang)
        user_prompt = self._build_user_prompt(input)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        logger.info(
            "repo_analysis_structured_attempt",
            extra={
                "event": "repo_analysis_structured_attempt",
                "correlation_id": correlation_id,
                "full_name": input.full_name,
                "max_attempts": max_attempts,
            },
        )

        try:
            structured_llm = cast("StructuredLLMServiceProtocol", self._llm)
            result = await structured_llm.chat_structured(
                messages,
                response_model=RepoAnalysis,
                max_retries=max_attempts,
                temperature=0.1,
            )
        except Exception as exc:
            logger.warning(
                "repo_analysis_structured_failed",
                extra={
                    "event": "repo_analysis_structured_failed",
                    "correlation_id": correlation_id,
                    "full_name": input.full_name,
                    "error": str(exc),
                },
            )
            await self._persist(
                correlation_id=correlation_id,
                attempt_index=1,
                attempt_trigger="agent",
                response_text="",
                status="error",
                error_text=str(exc),
                structured_output_used=True,
                request_messages=messages,
            )
            return None

        parsed: RepoAnalysis = cast("RepoAnalysis", result.parsed)
        await self._persist(
            correlation_id=correlation_id,
            attempt_index=max(1, result.retry_count + 1),
            attempt_trigger="agent",
            response_text=parsed.model_dump_json(),
            status="success",
            error_text=None,
            model_name=result.model_used,
            tokens_prompt=result.tokens_prompt,
            tokens_completion=result.tokens_completion,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            structured_output_used=True,
            request_messages=messages,
            response_json=parsed.model_dump(mode="json"),
        )
        logger.info(
            "repo_analysis_structured_success",
            extra={
                "event": "repo_analysis_structured_success",
                "correlation_id": correlation_id,
                "full_name": input.full_name,
                "attempt_index": max(1, result.retry_count + 1),
                "confidence": parsed.confidence,
            },
        )
        return parsed

    async def _analyze_legacy(
        self,
        input: RepoAnalysisInput,
        *,
        chosen_lang: Literal["en", "ru"],
        correlation_id: str,
        max_attempts: int,
    ) -> RepoAnalysis | None:
        """Run the legacy raw-text repair loop for non-structured clients."""
        system_prompt = self._load_system_prompt(chosen_lang)
        user_prompt = self._build_user_prompt(input)
        previous_error: str | None = None

        for attempt_index in range(1, max_attempts + 1):
            attempt_trigger = "initial" if attempt_index == 1 else "repair_loop"
            effective_prompt = (
                self._prepend_correction(user_prompt, previous_error)
                if previous_error
                else user_prompt
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": effective_prompt},
            ]

            logger.info(
                "repo_analysis_attempt",
                extra={
                    "event": "repo_analysis_attempt",
                    "correlation_id": correlation_id,
                    "full_name": input.full_name,
                    "attempt_index": attempt_index,
                    "attempt_trigger": attempt_trigger,
                },
            )

            raw_response: str = ""
            try:
                legacy_llm = cast("LLMServiceProtocol", self._llm)
                raw_response = await legacy_llm.call(
                    system_prompt=system_prompt,
                    user_prompt=effective_prompt,
                    correlation_id=correlation_id,
                )
            except Exception as exc:
                logger.error(
                    "repo_analysis_llm_error",
                    extra={
                        "event": "repo_analysis_llm_error",
                        "correlation_id": correlation_id,
                        "full_name": input.full_name,
                        "attempt_index": attempt_index,
                        "error": str(exc),
                    },
                )
                previous_error = f"LLM call failed: {exc}"
                await self._persist(
                    correlation_id=correlation_id,
                    attempt_index=attempt_index,
                    attempt_trigger=attempt_trigger,
                    response_text=raw_response,
                    status="error",
                    error_text=str(exc),
                    request_messages=messages,
                )
                continue

            try:
                result = parse_and_validate_repo_analysis(raw_response)
            except (ValidationError, Exception) as exc:
                error_msg = str(exc)
                logger.warning(
                    "repo_analysis_validation_failed",
                    extra={
                        "event": "repo_analysis_validation_failed",
                        "correlation_id": correlation_id,
                        "full_name": input.full_name,
                        "attempt_index": attempt_index,
                        "error": error_msg,
                    },
                )
                previous_error = error_msg
                await self._persist(
                    correlation_id=correlation_id,
                    attempt_index=attempt_index,
                    attempt_trigger=attempt_trigger,
                    response_text=raw_response,
                    status="success",
                    error_text=None,
                    request_messages=messages,
                )
                continue

            await self._persist(
                correlation_id=correlation_id,
                attempt_index=attempt_index,
                attempt_trigger=attempt_trigger,
                response_text=raw_response,
                status="success",
                error_text=None,
                request_messages=messages,
                response_json=result.model_dump(mode="json"),
            )

            logger.info(
                "repo_analysis_success",
                extra={
                    "event": "repo_analysis_success",
                    "correlation_id": correlation_id,
                    "full_name": input.full_name,
                    "attempt_index": attempt_index,
                    "confidence": result.confidence,
                },
            )
            return result

        logger.error(
            "repo_analysis_failed",
            extra={
                "event": "repo_analysis_failed",
                "correlation_id": correlation_id,
                "full_name": input.full_name,
                "max_attempts": max_attempts,
            },
        )
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_system_prompt(self, lang: Literal["en", "ru"]) -> str:
        prompt_file = _PROMPT_DIR / f"repo_analysis_system_{lang}.txt"
        try:
            return read_prompt_text(prompt_file)
        except OSError as exc:
            logger.warning(
                "repo_analysis_prompt_load_failed",
                extra={"lang": lang, "path": str(prompt_file), "error": str(exc)},
            )
            return (
                "You are a software repository analyst. "
                "Return ONLY a valid JSON object matching the RepoAnalysis schema."
            )

    @staticmethod
    def _build_user_prompt(input: RepoAnalysisInput) -> str:
        return (
            "Analyse the repository metadata inside the untrusted-source boundary and "
            "return a JSON object that strictly matches the RepoAnalysis schema.\n\n"
            + wrap_untrusted_source(json.dumps(input.model_dump(), ensure_ascii=False, indent=2))
        )

    @staticmethod
    def _prepend_correction(base_prompt: str, error: str) -> str:
        preamble = (
            "Your previous output failed validation:\n"
            f"{error}\n\n"
            "Fix the issues above and re-emit valid JSON that matches the schema.\n\n"
        )
        return preamble + base_prompt

    async def _persist(
        self,
        *,
        correlation_id: str,
        attempt_index: int,
        attempt_trigger: str,
        response_text: str,
        status: str,
        error_text: str | None,
        model_name: str | None = None,
        tokens_prompt: int | None = None,
        tokens_completion: int | None = None,
        cost_usd: float | None = None,
        latency_ms: int | None = None,
        structured_output_used: bool = False,
        request_messages: list[dict[str, Any]] | None = None,
        response_json: Any = None,
    ) -> None:
        if self._llm_repo is None:
            return
        await persist_agent_llm_call(
            self._llm_repo,
            request_id=self._request_id,
            endpoint="repo_analysis",
            model=model_name or self._model_name,
            status=status,
            response_text=response_text,
            latency_ms=latency_ms,
            error=Exception(error_text) if error_text else None,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            cost_usd=cost_usd,
            attempt_index=attempt_index,
            attempt_trigger=attempt_trigger,
            correlation_id=correlation_id,
            structured_output_used=structured_output_used,
            provider=getattr(self._llm, "provider_name", None),
            request_messages=request_messages,
            response_json=response_json,
        )
