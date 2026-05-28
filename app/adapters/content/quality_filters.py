"""Quality filtering logic for extracted content."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from app.adapters.external.firecrawl.models import FirecrawlResult
    from app.adapters.llm.protocol import LLMClientProtocol

from app.core.html_utils import clean_markdown_article_text, html_to_text
from app.core.logging_utils import get_logger
from app.prompts.file_cache import read_prompt_text

logger = get_logger(__name__)

LowValueReason = Literal[
    "empty_after_cleaning",
    "overlay_content_detected",
    "content_too_short",
    "content_low_variation",
    "content_high_repetition",
    "nav_stub_detected",
]


def extract_content_text_candidates(crawl: FirecrawlResult) -> list[str]:
    """Build the candidate pool for low-value detection — markdown first, HTML as fallback."""
    text_candidates: list[str] = []
    if crawl.content_markdown and crawl.content_markdown.strip():
        text_candidates.append(clean_markdown_article_text(crawl.content_markdown))
    if crawl.content_html and crawl.content_html.strip():
        text_candidates.append(html_to_text(crawl.content_html))
    return text_candidates


def best_content_text(crawl: FirecrawlResult) -> str:
    """Select the longest candidate — length is a proxy for content richness at this stage."""
    candidates = [t for t in extract_content_text_candidates(crawl) if t and t.strip()]
    return max(candidates, key=len, default="")


def _detect_low_value_text(text: str) -> dict[str, Any] | None:
    primary_text = text
    normalized = re.sub(r"\s+", " ", primary_text).strip()

    words_raw = re.findall(r"[\w']+", normalized)
    words = [w.lower() for w in words_raw if w]
    word_count = len(words)
    unique_word_count = len(set(words))

    top_word: str | None = None
    top_ratio = 0.0
    if words:
        counter = Counter(words)
        top_word, top_count = counter.most_common(1)[0]
        top_ratio = top_count / word_count if word_count else 0.0

    overlay_terms = {
        "accept",
        "close",
        "cookie",
        "cookies",
        "consent",
        "login",
        "signin",
        "signup",
        "subscribe",
    }
    overlay_ratio = sum(1 for w in words if w in overlay_terms) / word_count if word_count else 0.0

    # Count "substantive sentences" -- sequences of 10+ words ending
    # with sentence-terminal punctuation (.!?) in the normalized text.
    substantive_sentence_count = len(
        [s for s in re.split(r"[.!?]+", normalized) if len(re.findall(r"[\w']+", s)) >= 10]
    )

    reason: LowValueReason | None = None
    if not normalized or word_count == 0:
        reason = "empty_after_cleaning"
    elif overlay_ratio >= 0.7 and len(normalized) < 600:
        reason = "overlay_content_detected"
    elif len(normalized) < 48 and word_count <= 2:
        reason = "content_too_short"
    elif len(normalized) < 120 and (
        unique_word_count <= 3 or (word_count >= 4 and top_ratio >= 0.8)
    ):
        reason = "content_low_variation"
    elif word_count >= 6 and top_ratio >= 0.92:
        reason = "content_high_repetition"
    elif word_count < 100 and substantive_sentence_count < 2:
        reason = "nav_stub_detected"

    if reason:
        return {
            "reason": reason,
            "preview": normalized[:200],
            "metrics": {
                "char_length": len(normalized),
                "word_count": word_count,
                "unique_word_count": unique_word_count,
                "top_word": top_word,
                "top_ratio": top_ratio,
                "overlay_ratio": overlay_ratio,
                "substantive_sentence_count": substantive_sentence_count,
            },
        }
    return None


def detect_low_value_content(crawl: FirecrawlResult) -> dict[str, Any] | None:
    """Detect low-value Firecrawl responses that should halt processing.

    Markdown and HTML are evaluated as separate extraction candidates. A useful
    HTML body can therefore rescue a thin or low-value markdown field.
    """

    candidates = [t for t in extract_content_text_candidates(crawl) if t and t.strip()]
    if not candidates:
        return _detect_low_value_text("")

    issues: list[dict[str, Any]] = []
    for candidate in candidates:
        issue = _detect_low_value_text(candidate)
        if issue is None:
            return None
        issues.append(issue)

    return max(issues, key=lambda item: item["metrics"]["char_length"], default=None)


def is_gray_zone_for_llm_check(reason: LowValueReason, metrics: dict[str, Any]) -> bool:
    """Determine if the heuristic verdict is ambiguous enough to warrant LLM review."""
    if reason != "nav_stub_detected":
        return False
    wc = metrics.get("word_count", 0)
    ssc = metrics.get("substantive_sentence_count", 0)
    return bool(15 <= wc <= 150 and ssc <= 3)


_QUALITY_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "quality_check_system.txt"


class _QualityVerdict(BaseModel):
    classification: str  # "real_content" | "stub" | ...
    confidence: float = 0.0


def _load_quality_system_prompt() -> str:
    # read_prompt_text caches per process, so the file is read at most once.
    return read_prompt_text(_QUALITY_PROMPT_PATH, strip=True)


def _parse_quality_verdict(response_text: str) -> _QualityVerdict | None:
    """Parse LLM response text into a QualityVerdict, returning None on any failure."""
    import json as _json

    from app.core.json_utils import extract_json

    parsed_data = extract_json(response_text)
    if not isinstance(parsed_data, dict):
        try:
            parsed_data = _json.loads(response_text)
        except (ValueError, TypeError):
            return None

    if not isinstance(parsed_data, dict):
        return None

    try:
        return _QualityVerdict(**parsed_data)
    except (TypeError, ValueError):
        return None


async def classify_content_quality_llm(
    text_preview: str,
    metrics: dict[str, Any],
    llm_client: LLMClientProtocol,
    *,
    flash_model: str,
    flash_fallback_models: tuple[str, ...] | list[str],
    timeout_sec: float = 3.0,
    confidence_threshold: float = 0.7,
    request_id: int | None = None,
) -> tuple[bool, Any]:
    """Ask LLM whether extracted text is real content or a stub.

    Returns (is_stub, llm_result). On any failure, defers to the heuristic
    verdict by returning (True, None).
    """
    system_prompt = _load_quality_system_prompt()
    user_message = (
        f"Text (first 500 chars): {text_preview[:500]}\n"
        f"Word count: {metrics.get('word_count', 0)}\n"
        f"Substantive sentences: {metrics.get('substantive_sentence_count', 0)}"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        llm_result = await asyncio.wait_for(
            llm_client.chat(
                messages,
                temperature=0.0,
                max_tokens=50,
                model_override=flash_model,
                request_id=request_id,
            ),
            timeout=timeout_sec,
        )
    except TimeoutError:
        logger.warning("quality_llm_timeout", extra={"request_id": request_id})
        return True, None
    except Exception:
        logger.warning("quality_llm_error", extra={"request_id": request_id}, exc_info=True)
        return True, None

    verdict = _parse_quality_verdict(llm_result.response_text or "")
    if verdict is None:
        logger.warning("quality_llm_parse_error", extra={"request_id": request_id})
        return True, llm_result

    if verdict.classification == "real_content" and verdict.confidence >= confidence_threshold:
        return False, llm_result
    return True, llm_result
