"""LLM fallback that files a repository into one of the user's star lists.

The primary path is nearest-neighbour voting over repositories the user has
already filed (see
:mod:`app.application.use_cases.suggest_star_lists`). This service runs only when
the neighbours disagree or there are none — a brand-new interest has nothing to
be similar to.

The model is constrained to the names it was given and its answer is re-checked
against them here, because GitHub caps a user at 32 lists: a hallucinated name
cannot be created, and writing it would be interpreted as "no list".
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from app.agents.llm_call_persistence import persist_agent_llm_call
from app.core.logging_utils import get_logger
from app.core.star_list_suggestion_schema import StarListCandidate, StarListChoice
from app.prompts.file_cache import read_prompt_text

if TYPE_CHECKING:
    from app.application.ports.llm_client import LLMClientProtocol
    from app.application.ports.requests import LLMRepositoryPort

logger = get_logger(__name__)
PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"

_README_BUDGET = 2000
_ENDPOINT = "star_list_classifier"


class StarListClassifierService:
    """One bounded structured LLM call that picks a list, or none."""

    def __init__(
        self,
        *,
        llm_client: LLMClientProtocol,
        llm_repo: LLMRepositoryPort | None = None,
        lang: str = "en",
    ) -> None:
        self._llm = llm_client
        self._llm_repo = llm_repo
        self._lang = lang

    async def classify(
        self,
        *,
        candidates: list[StarListCandidate],
        full_name: str,
        description: str | None,
        language: str | None,
        topics: list[str],
        readme_excerpt: str | None,
        correlation_id: str | None = None,
    ) -> StarListChoice | None:
        """Return the model's pick, or ``None`` when it cannot be trusted.

        ``None`` covers three cases that a caller treats identically: nothing to
        choose from, the call failed, and the answer named a list that does not
        exist.
        """
        if not candidates:
            return None

        prompt = self._build_prompt(
            candidates=candidates,
            full_name=full_name,
            description=description,
            language=language,
            topics=topics,
            readme_excerpt=readme_excerpt,
        )
        model = getattr(self._llm, "_model", "unknown")

        try:
            result = await self._llm.chat_structured(
                [{"role": "user", "content": prompt}],
                response_model=StarListChoice,
                max_retries=2,
                temperature=0.0,
                max_tokens=350,
            )
        except Exception as exc:
            logger.warning(
                "star_list_classifier_llm_error",
                extra={"full_name": full_name, "correlation_id": correlation_id, "error": str(exc)},
            )
            # Operating Rule #3: every billed LLM call -- success AND failure --
            # is persisted. Best-effort; never changes the outcome.
            await persist_agent_llm_call(
                self._llm_repo,
                request_id=None,
                endpoint=_ENDPOINT,
                model=model,
                status="error",
                error=exc,
                attempt_trigger="agent",
                correlation_id=correlation_id,
                structured_output_used=True,
            )
            return None

        await persist_agent_llm_call(
            self._llm_repo,
            request_id=None,
            endpoint=_ENDPOINT,
            model=model,
            status="success",
            result=result,
            attempt_trigger="agent",
            correlation_id=correlation_id,
            structured_output_used=True,
        )

        choice: StarListChoice = result.parsed
        picked = choice.list_name.strip()
        if not picked:
            return choice

        # The prompt forbids inventing a name, so a mismatch is a hallucination.
        # Passing it on would clear the repository's membership instead of
        # setting it, which is the opposite of what the caller asked for.
        allowed = {candidate.name for candidate in candidates}
        if picked not in allowed:
            logger.warning(
                "star_list_classifier_hallucinated_list",
                extra={
                    "full_name": full_name,
                    "correlation_id": correlation_id,
                    "picked": picked,
                },
            )
            return None

        return StarListChoice(
            list_name=picked,
            confidence=choice.confidence,
            reason=choice.reason,
        )

    def _build_prompt(
        self,
        *,
        candidates: list[StarListCandidate],
        full_name: str,
        description: str | None,
        language: str | None,
        topics: list[str],
        readme_excerpt: str | None,
    ) -> str:
        rendered_lists = "\n".join(
            f"{candidate.name} — {candidate.description}".rstrip(" —") for candidate in candidates
        )
        template = _load_prompt(self._lang)
        return template.format(
            lists=rendered_lists,
            full_name=full_name,
            language=language or "unknown",
            topics=", ".join(topics) if topics else "none",
            description=(description or "none")[:1000],
            readme=(readme_excerpt or "none")[:_README_BUDGET],
        )


def _load_prompt(lang: str) -> str:
    safe_lang = "ru" if lang.startswith("ru") else "en"
    return read_prompt_text(PROMPT_DIR / f"star_list_classifier_{safe_lang}.txt", strip=True)
