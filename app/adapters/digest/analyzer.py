"""Digest analyzer -- lightweight LLM analysis for channel posts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from app.agents.llm_call_persistence import persist_agent_llm_call
from app.core.logging_utils import get_logger
from app.infrastructure.persistence.digest_store import DigestStore
from app.infrastructure.persistence.repositories.llm_repository import LLMRepositoryAdapter
from app.prompts.file_cache import read_prompt_text

if TYPE_CHECKING:
    from app.adapters.llm.protocol import LLMClientProtocol
    from app.application.ports.requests import LLMRepositoryPort
    from app.config import AppConfig

logger = get_logger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

VALID_CONTENT_TYPES = {"news", "tutorial", "opinion", "announcement", "other"}
ANALYSIS_FAILED_STATUS = "analysis_failed"


class _DigestPostAnalysis(BaseModel):
    real_topic: str = ""
    tldr: str = ""
    key_insights: list[str] = Field(default_factory=list)
    relevance_score: float = 0.5
    content_type: str = "other"
    is_ad: bool = False


class DigestAnalyzer:
    """Runs lightweight LLM analysis on channel posts with concurrency control."""

    def __init__(
        self,
        cfg: AppConfig,
        llm_client: LLMClientProtocol,
        llm_repo: LLMRepositoryPort | None = None,
    ) -> None:
        self._cfg = cfg
        self._llm = llm_client
        self._semaphore = asyncio.Semaphore(cfg.digest.concurrency)
        self._store = DigestStore()
        self._llm_repo = llm_repo

    async def analyze_posts(
        self,
        posts: list[dict[str, Any]],
        correlation_id: str,
        lang: str = "en",
    ) -> list[dict[str, Any]]:
        """Analyze a batch of posts concurrently with semaphore control.

        Args:
            posts: List of post dicts from ChannelReader.
            correlation_id: Correlation ID for tracing.
            lang: Language for prompt selection (en/ru).

        Returns:
            List of analysis result dicts.
        """
        tasks = [self._analyze_single(post, correlation_id, lang) for post in posts]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        analyzed: list[dict[str, Any]] = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.warning(
                    "digest_analysis_single_failed",
                    extra={
                        "cid": correlation_id,
                        "post_url": posts[i].get("url"),
                        "error": str(result),
                    },
                )
                continue
            if result is not None and isinstance(result, dict):
                analyzed.append(result)

        logger.info(
            "digest_analysis_batch_complete",
            extra={
                "cid": correlation_id,
                "total": len(posts),
                "analyzed": len(analyzed),
            },
        )
        return analyzed

    async def _cached_analysis(self, post: dict[str, Any]) -> dict[str, Any] | None:
        """Return existing analysis from DB if the post was already analyzed."""
        return await self._store.async_find_cached_analysis(post)

    @staticmethod
    def _parse_and_validate_llm_response(
        raw: dict[str, Any], correlation_id: str
    ) -> dict[str, Any] | None:
        """Validate normalized fields from a parsed LLM response dict.

        Returns a dict with normalized fields, or None if validation fails.
        """
        parsed = raw

        real_topic = str(parsed.get("real_topic", "")).strip()
        tldr = str(parsed.get("tldr", "")).strip()
        if not real_topic or not tldr:
            logger.warning(
                "digest_analysis_missing_fields",
                extra={"cid": correlation_id},
            )
            return None

        key_insights = parsed.get("key_insights")
        if not isinstance(key_insights, list):
            key_insights = []

        relevance_score = parsed.get("relevance_score", 0.5)
        try:
            relevance_score = max(0.0, min(1.0, float(relevance_score)))
        except (TypeError, ValueError):
            relevance_score = 0.5

        content_type = str(parsed.get("content_type", "other")).strip().lower()
        if content_type not in VALID_CONTENT_TYPES:
            content_type = "other"

        is_ad = bool(parsed.get("is_ad", False))

        return {
            "real_topic": real_topic,
            "tldr": tldr,
            "key_insights": key_insights,
            "relevance_score": relevance_score,
            "content_type": content_type,
            "is_ad": is_ad,
        }

    async def _persist_analysis(self, post: dict[str, Any], fields: dict[str, Any]) -> None:
        """Persist LLM analysis results to the DB for the given post."""
        await self._store.async_persist_analysis(post, fields)

    def _llm_call_repo(self) -> LLMRepositoryPort | None:
        """Resolve the LLM-call repository lazily from the store's database.

        Explicitly injectable (tests); otherwise it reuses the same runtime
        database the DigestStore persists analysis to. Returns None when no
        database is available -- persist_agent_llm_call then no-ops, so analysis
        never fails because call-metadata persistence could not be set up.
        """
        if self._llm_repo is None:
            try:
                self._llm_repo = LLMRepositoryAdapter(self._store._database())
            except Exception:
                return None
        return self._llm_repo

    async def _analyze_single(
        self,
        post: dict[str, Any],
        correlation_id: str,
        lang: str,
    ) -> dict[str, Any] | None:
        """Analyze a single post under the concurrency semaphore."""
        # Hold the concurrency semaphore across the whole operation, including the
        # cache lookup, so a large batch cannot fan out N concurrent DB queries
        # before any LLM gating takes effect.
        async with self._semaphore:
            cached = await self._cached_analysis(post)
            if cached is not None:
                logger.debug(
                    "digest_analysis_cache_hit",
                    extra={"cid": correlation_id, "msg_id": post.get("message_id")},
                )
                return cached

            prompt_template = self._load_prompt(lang)
            user_prompt = prompt_template.replace("{post_text}", post["text"][:4000])

            messages: list[dict[str, Any]] = [
                {"role": "user", "content": user_prompt},
            ]

            model = getattr(self._llm, "_model", "unknown")
            try:
                result = await self._llm.chat_structured(
                    messages,
                    response_model=_DigestPostAnalysis,
                    max_retries=3,
                    temperature=0.1,
                    max_tokens=500,
                )
            except Exception as exc:
                logger.warning(
                    "digest_llm_error",
                    extra={"cid": correlation_id, "error": str(exc)},
                )
                await persist_agent_llm_call(
                    self._llm_call_repo(),
                    request_id=None,
                    endpoint="digest_analysis",
                    model=model,
                    status="error",
                    error=exc,
                    correlation_id=correlation_id,
                    structured_output_used=True,
                )
                return _fallback_analysis(post, reason=ANALYSIS_FAILED_STATUS)

            # Record the billed LLM call's cost/tokens/model even when the
            # structured payload later fails validation -- the call already ran.
            await persist_agent_llm_call(
                self._llm_call_repo(),
                request_id=None,
                endpoint="digest_analysis",
                model=model,
                status="success",
                result=result,
                correlation_id=correlation_id,
                structured_output_used=True,
            )

            fields = self._parse_and_validate_llm_response(
                result.parsed.model_dump(), correlation_id
            )
            if fields is None:
                return _fallback_analysis(post, reason=ANALYSIS_FAILED_STATUS)

            await self._persist_analysis(post, fields)

            return {**post, **fields}

    @staticmethod
    def _load_prompt(lang: str) -> str:
        """Load the digest analysis prompt for the given language."""
        safe_lang = "ru" if lang.startswith("ru") else "en"
        path = PROMPT_DIR / f"digest_analysis_{safe_lang}.txt"
        try:
            return read_prompt_text(path, strip=True)
        except FileNotFoundError:
            # Fallback to English
            fallback = PROMPT_DIR / "digest_analysis_en.txt"
            return read_prompt_text(fallback, strip=True)


def _fallback_analysis(post: dict[str, Any], *, reason: str) -> dict[str, Any]:
    text = str(post.get("text") or "").strip()
    title = str(post.get("title") or "").strip()
    topic = title or (text[:80].strip() if text else "Unanalyzed post")
    if len(topic) == 80 and len(text) > 80:
        topic = f"{topic.rstrip()}..."
    excerpt = text[:240].strip()
    if len(excerpt) == 240 and len(text) > 240:
        excerpt = f"{excerpt.rstrip()}..."
    return {
        **post,
        "real_topic": topic,
        "tldr": excerpt or "Analysis failed; no post text was available.",
        "key_insights": [],
        "relevance_score": 0.5,
        "content_type": "other",
        "is_ad": False,
        "analysis_status": reason,
    }
