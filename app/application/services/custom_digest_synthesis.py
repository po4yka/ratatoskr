"""Cited synthesis for custom digests built from existing summaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, Field, field_validator

from app.agents.llm_call_persistence import persist_agent_llm_call
from app.core.content_cleaner import wrap_untrusted_source
from app.prompts.file_cache import read_prompt_text

_PROMPT_DIR = Path(__file__).parents[2] / "prompts"


class DigestClaim(BaseModel):
    """One digest claim with summary citations."""

    text: str = Field(min_length=1)
    summary_ids: list[int] = Field(min_length=1)


class DigestPerspective(BaseModel):
    """One source-specific perspective that complements the other claims."""

    text: str = Field(min_length=1)
    summary_ids: list[int] = Field(min_length=1)


class DigestDisagreement(DigestPerspective):
    """A material disagreement that must cite at least two selected summaries."""

    summary_ids: list[int] = Field(min_length=2)

    @field_validator("summary_ids")
    @classmethod
    def require_distinct_sources(cls, value: list[int]) -> list[int]:
        if len(set(value)) < 2:
            msg = "Disagreements require two distinct summary citations"
            raise ValueError(msg)
        return value


_CitedItem = TypeVar("_CitedItem", DigestClaim, DigestPerspective, DigestDisagreement)


class CustomDigestSynthesis(BaseModel):
    """Structured content for a synthesized custom digest."""

    claims: list[DigestClaim] = Field(min_length=1)
    disagreements: list[DigestDisagreement] = Field(default_factory=list)
    complementary_perspectives: list[DigestPerspective] = Field(min_length=1)
    reading_order: list[int] = Field(min_length=1)
    titles: dict[int, str] = Field(default_factory=dict)

    def to_markdown(self) -> str:
        """Render cited structured synthesis as a portable Markdown digest."""
        sections = [
            "## Key claims",
            *[f"- {claim.text} {_citations(claim.summary_ids)}" for claim in self.claims],
            "## Disagreements",
            *(
                [f"- {item.text} {_citations(item.summary_ids)}" for item in self.disagreements]
                or ["- No material disagreement identified in the selected summaries."]
            ),
            "## Complementary perspectives",
            *[
                f"- {item.text} {_citations(item.summary_ids)}"
                for item in self.complementary_perspectives
            ],
            "## Suggested reading order",
            *[
                f"{position}. {self.titles.get(summary_id, f'Summary {summary_id}')} "
                f"[summary:{summary_id}]"
                for position, summary_id in enumerate(self.reading_order, start=1)
            ],
        ]
        return "\n".join(sections)


class _DigestSynthesisLLMResponse(BaseModel):
    claims: list[DigestClaim] = Field(default_factory=list)
    disagreements: list[DigestDisagreement] = Field(default_factory=list)
    complementary_perspectives: list[DigestPerspective] = Field(default_factory=list)
    reading_order: list[int] = Field(default_factory=list)


class CustomDigestSynthesizer:
    """Create a cited custom-digest synthesis with a safe deterministic fallback."""

    def __init__(self, *, llm_client: Any | None = None, llm_repo: Any | None = None) -> None:
        self._llm = llm_client
        self._llm_repo = llm_repo

    async def synthesize(
        self,
        summaries: list[dict[str, Any]],
        *,
        language: str = "en",
        correlation_id: str | None = None,
    ) -> CustomDigestSynthesis:
        """Build a structured synthesis with source IDs validated against the selected summaries."""
        fallback = self._fallback_synthesis(summaries)
        if self._llm is None:
            return fallback

        request_id = _first_request_id(summaries)
        model = getattr(self._llm, "_model", "unknown")
        try:
            result = await self._llm.chat_structured(
                [
                    {"role": "system", "content": self._load_prompt(language)},
                    {
                        "role": "user",
                        "content": (
                            "Synthesize only from the selected summaries inside the "
                            "untrusted-source boundary.\n\n"
                            + wrap_untrusted_source(self._build_context(summaries))
                        ),
                    },
                ],
                response_model=_DigestSynthesisLLMResponse,
                max_retries=3,
                max_tokens=1800,
                temperature=0.2,
                request_id=request_id,
            )
        except Exception as exc:
            await persist_agent_llm_call(
                self._llm_repo,
                request_id=request_id,
                endpoint="custom_digest_synthesis",
                model=model,
                status="error",
                error=exc,
                correlation_id=correlation_id,
                structured_output_used=True,
            )
            return fallback

        await persist_agent_llm_call(
            self._llm_repo,
            request_id=request_id,
            endpoint="custom_digest_synthesis",
            model=model,
            status="success",
            result=result,
            correlation_id=correlation_id,
            structured_output_used=True,
        )
        return self._validated_llm_synthesis(result.parsed, fallback)

    @staticmethod
    def _fallback_synthesis(summaries: list[dict[str, Any]]) -> CustomDigestSynthesis:
        """Build deterministic cited content when no structured LLM result is available."""
        claims: list[DigestClaim] = []
        perspectives: list[DigestPerspective] = []
        titles: dict[int, str] = {}
        reading_scores: list[tuple[int, int]] = []

        for summary in summaries:
            summary_id = int(summary["id"])
            payload = _mapping(summary.get("json_payload"))
            metadata = _mapping(payload.get("metadata"))
            title = str(metadata.get("title") or f"Summary {summary_id}").strip()
            titles[summary_id] = title or f"Summary {summary_id}"
            ideas = _text_list(payload.get("key_ideas"))[:2]
            if not ideas:
                fallback = str(payload.get("tldr") or payload.get("summary_250") or "").strip()
                if fallback:
                    ideas = [fallback]
            for idea in ideas:
                claims.append(DigestClaim(text=idea, summary_ids=[summary_id]))
            perspective = ideas[0] if ideas else "Adds source context without a concise claim."
            perspectives.append(
                DigestPerspective(
                    text=f"{titles[summary_id]}: {perspective}", summary_ids=[summary_id]
                )
            )
            reading_scores.append((summary_id, len(ideas)))

        if not claims:
            msg = "Selected summaries do not contain usable digest content"
            raise ValueError(msg)
        reading_order = [
            summary_id
            for summary_id, _score in sorted(reading_scores, key=lambda item: (-item[1], item[0]))
        ]
        return CustomDigestSynthesis(
            claims=claims,
            complementary_perspectives=perspectives,
            reading_order=reading_order,
            titles=titles,
        )

    @staticmethod
    def _validated_llm_synthesis(
        parsed: _DigestSynthesisLLMResponse,
        fallback: CustomDigestSynthesis,
    ) -> CustomDigestSynthesis:
        valid_ids = set(fallback.titles)
        claims = _valid_cited_items(parsed.claims, valid_ids)
        if not claims:
            return fallback
        perspectives = _valid_cited_items(parsed.complementary_perspectives, valid_ids)
        disagreements = _valid_disagreements(parsed.disagreements, valid_ids)
        reading_order = _complete_reading_order(
            parsed.reading_order, fallback.reading_order, valid_ids
        )
        return CustomDigestSynthesis(
            claims=claims,
            disagreements=disagreements,
            complementary_perspectives=perspectives or fallback.complementary_perspectives,
            reading_order=reading_order or fallback.reading_order,
            titles=fallback.titles,
        )

    @staticmethod
    def _build_context(summaries: list[dict[str, Any]]) -> str:
        sources = []
        for summary in summaries:
            payload = _mapping(summary.get("json_payload"))
            metadata = _mapping(payload.get("metadata"))
            request = _mapping(summary.get("request"))
            sources.append(
                {
                    "summary_id": int(summary["id"]),
                    "title": metadata.get("title") or request.get("input_url"),
                    "summary": _truncate(
                        str(payload.get("summary_1000") or payload.get("summary_250") or ""), 1600
                    ),
                    "key_ideas": _text_list(payload.get("key_ideas"))[:8],
                    "topic_tags": _text_list(payload.get("topic_tags"))[:8],
                }
            )
        return json.dumps({"sources": sources}, ensure_ascii=False)

    @staticmethod
    def _load_prompt(language: str) -> str:
        suffix = "ru" if language == "ru" else "en"
        return read_prompt_text(
            _PROMPT_DIR / f"custom_digest_synthesis_system_{suffix}.txt", strip=True
        )


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _citations(summary_ids: list[int]) -> str:
    return " ".join(f"[summary:{summary_id}]" for summary_id in summary_ids)


def _valid_cited_items(
    items: list[_CitedItem],
    valid_ids: set[int],
) -> list[_CitedItem]:
    valid_items: list[_CitedItem] = []
    for item in items:
        summary_ids = [summary_id for summary_id in item.summary_ids if summary_id in valid_ids]
        if summary_ids:
            valid_items.append(item.model_copy(update={"summary_ids": summary_ids}))
    return valid_items


def _valid_disagreements(
    items: list[DigestDisagreement], valid_ids: set[int]
) -> list[DigestDisagreement]:
    valid_items: list[DigestDisagreement] = []
    for item in items:
        summary_ids = list(
            dict.fromkeys(summary_id for summary_id in item.summary_ids if summary_id in valid_ids)
        )
        if len(summary_ids) >= 2:
            valid_items.append(item.model_copy(update={"summary_ids": summary_ids}))
    return valid_items


def _complete_reading_order(
    reading_order: list[int],
    fallback_order: list[int],
    valid_ids: set[int],
) -> list[int]:
    result: list[int] = []
    for summary_id in reading_order:
        if summary_id in valid_ids and summary_id not in result:
            result.append(summary_id)
    for summary_id in fallback_order:
        if summary_id in valid_ids and summary_id not in result:
            result.append(summary_id)
    return result


def _first_request_id(summaries: list[dict[str, Any]]) -> int | None:
    for summary in summaries:
        request = _mapping(summary.get("request"))
        request_id = request.get("id")
        if isinstance(request_id, int):
            return request_id
    return None


def _truncate(text: str, max_length: int) -> str:
    normalized = text.strip()
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 1].rstrip() + "…"
