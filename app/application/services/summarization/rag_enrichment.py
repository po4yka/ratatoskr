"""RAG-field enrichment + LLM metadata-completion for the content-only path (audit #5).

Ports the two summary-completion steps the legacy
:meth:`~app.adapters.content.llm_summarizer_metadata.LLMSummaryMetadataHelper.ensure_summary_metadata`
ran but the T9 content-only graph path lost:

1. :func:`enrich_summary_rag_fields` -- the PURE
   :meth:`~app.adapters.content.llm_summarizer_semantic.LLMSemanticHelper.enrich_with_rag_fields`
   port. It attaches retrieval-optimized fields (``language``, ``article_id``,
   ``semantic_boosters``, ``query_expansion_keywords``, ``semantic_chunks``)
   derived purely from the summary + content via ``app.core`` helpers. No LLM, no
   ports -- only ``app.core.*`` + stdlib, legal from the application layer.

2. :func:`complete_summary_metadata_via_llm` -- the LLM metadata-completion port.
   When heuristic backfill leaves ``title`` / ``author`` / ``published_at`` /
   ``last_updated`` blank, it asks the LLM (via the ``LLMClientProtocol`` port --
   never a concrete adapter) to fill them, returning the values AND a serializable
   ``llm_calls`` record so the caller persists the call (persist-everything, rule 3).

``application-no-outward``: imports only ``app.core.*`` and ``app.application.*``.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from app.core.async_utils import raise_if_cancelled
from app.core.call_status import CallStatus
from app.core.json_utils import extract_json
from app.core.logging_utils import get_logger
from app.core.summary_contract import cap_text, extract_keywords_tfidf, normalize_whitespace

if TYPE_CHECKING:
    from app.application.ports.llm_client import LLMClientProtocol

logger = get_logger(__name__)

# Metadata fields the LLM-completion step targets (verbatim from
# LLMSummaryMetadataHelper._LLM_METADATA_FIELDS).
LLM_METADATA_FIELDS: tuple[str, ...] = ("title", "author", "published_at", "last_updated")

_SIMPLE_KEYWORD_STOP_WORDS = {
    "and",
    "the",
    "with",
    "from",
    "that",
    "this",
    "they",
    "been",
    "have",
    "were",
    "their",
    "will",
    "some",
    "который",
    "через",
    "между",
    "после",
    "перед",
    "было",
    "были",
    "есть",
    "будет",
    "этого",
    "чтобы",
}


# ---------------------------------------------------------------------------
# Step 1 -- pure RAG-field enrichment (no LLM, no ports)
# ---------------------------------------------------------------------------


async def enrich_summary_rag_fields(
    summary: dict[str, Any],
    *,
    content_text: str,
    chosen_lang: str | None,
    request_id: int | None,
) -> dict[str, Any]:
    """Attach RAG-optimized fields derived from content and summary (in-place).

    Verbatim port of ``LLMSemanticHelper.enrich_with_rag_fields`` -- purely
    computational (no LLM, no DB). Async only because the TF-IDF keyword extraction
    is offloaded to a worker thread (``asyncio.to_thread``, rule 6 -- no blocking
    CPU work on the event loop), exactly like the legacy helper. ``request_id`` may
    be ``None`` on the content-only path; it only seeds the fallback ``article_id``.
    """
    if not isinstance(summary, dict):
        return summary

    lang = chosen_lang or summary.get("language")
    summary["language"] = lang
    article_id = summary.get("article_id") or (str(request_id) if request_id is not None else None)
    summary["article_id"] = str(article_id) if article_id else None

    topics = [
        str(t).strip().lstrip("#")
        for t in summary.get("topic_tags", [])
        if isinstance(t, str) and str(t).strip()
    ]

    base_text = " ".join(
        [
            summary.get("summary_1000") or "",
            summary.get("summary_250") or "",
            summary.get("tldr") or "",
        ]
    )

    if not summary.get("semantic_boosters"):
        summary["semantic_boosters"] = _generate_semantic_boosters(base_text, summary)
    else:
        summary["semantic_boosters"] = summary.get("semantic_boosters", [])[:15]

    if not summary.get("query_expansion_keywords") or len(summary["query_expansion_keywords"]) < 20:
        summary["query_expansion_keywords"] = await _generate_query_expansion_keywords(
            summary, content_text or base_text
        )
    else:
        summary["query_expansion_keywords"] = summary.get("query_expansion_keywords", [])[:30]

    if not summary.get("semantic_chunks"):
        summary["semantic_chunks"] = _build_semantic_chunks(
            content_text,
            topics=topics,
            article_id=summary.get("article_id"),
            language=lang,
        )

    return summary


# ---------------------------------------------------------------------------
# Step 2 -- LLM metadata-completion (via the llm_client PORT)
# ---------------------------------------------------------------------------


async def complete_summary_metadata_via_llm(
    *,
    llm_client: LLMClientProtocol,
    content_text: str,
    fields: list[str],
    request_id: int | None,
    correlation_id: str | None,
    structured_output_mode: str | None = None,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """Ask the LLM to fill missing metadata fields when heuristics fail.

    Port of ``LLMSummaryMetadataHelper._generate_metadata_completion`` that calls the
    ``LLMClientProtocol`` port (not the concrete OpenRouter adapter). Returns
    ``(cleaned_fields, llm_call_record)`` where ``llm_call_record`` is a serializable
    ``llm_calls`` row (``attempt_trigger='graph_node'``) the caller appends to
    ``state['llm_calls']`` so the persist node writes it (persist-everything). The
    record is ``None`` when no call was made (no fields / empty content) or the call
    raised at transport level (already persisted by the adapter).
    """
    if not fields:
        return {}, None

    snippet = content_text[:6000].strip()
    if not snippet:
        return {}, None

    system_prompt = (
        "You extract article metadata and must respond with a strict JSON object. "
        "Do not add commentary. Use null when a field cannot be determined."
    )
    user_prompt = (
        "Provide the following metadata fields as JSON keys only: "
        f"{', '.join(fields)}.\n"
        "Base your answer on this article content.\n"
        "CONTENT START\n"
        f"{snippet}\n"
        "CONTENT END"
    )
    response_format = {
        "type": "json_object",
        "schema": {
            "name": "metadata_completion",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {field: {"type": ["string", "null"]} for field in fields},
                "required": list(fields),
            },
        },
    }
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        llm = await llm_client.chat(
            messages,
            temperature=0.2,
            max_tokens=512,
            top_p=0.9,
            request_id=request_id,
            response_format=response_format,
        )
    except Exception as exc:
        raise_if_cancelled(exc)
        logger.warning(
            "metadata_completion_call_failed",
            extra={"cid": correlation_id, "error": str(exc)},
        )
        return {}, None

    record = _llm_call_record(llm, request_id, structured_output_mode)

    if llm.status != CallStatus.OK:
        logger.warning(
            "metadata_completion_failed",
            extra={"cid": correlation_id, "status": llm.status, "error": llm.error_text},
        )
        return {}, record

    parsed = _parse_metadata_completion(
        getattr(llm, "response_json", None), getattr(llm, "response_text", None)
    )
    if not isinstance(parsed, dict):
        logger.warning("metadata_completion_unparsed", extra={"cid": correlation_id})
        return {}, record

    cleaned: dict[str, str] = {}
    for field in fields:
        raw_value = parsed.get(field)
        if isinstance(raw_value, str) and raw_value.strip():
            cleaned[field] = raw_value.strip()

    if cleaned:
        logger.info(
            "metadata_completion_success",
            extra={"cid": correlation_id, "fields": list(cleaned.keys())},
        )
    return cleaned, record


def _llm_call_record(
    llm: Any, request_id: int | None, structured_output_mode: str | None
) -> dict[str, Any]:
    """Build the serializable llm_calls record for the metadata-completion call."""
    status = getattr(llm, "status", None)
    status_str = status.value if hasattr(status, "value") else str(status) if status else "ok"
    return {
        "request_id": request_id,
        "provider": "openrouter",
        "model": getattr(llm, "model", None),
        "response_text": getattr(llm, "response_text", None),
        "tokens_prompt": getattr(llm, "tokens_prompt", None),
        "tokens_completion": getattr(llm, "tokens_completion", None),
        "cost_usd": getattr(llm, "cost_usd", None),
        "latency_ms": getattr(llm, "latency_ms", None),
        "status": status_str,
        "error_text": getattr(llm, "error_text", None),
        "structured_output_used": True,
        "structured_output_mode": structured_output_mode,
        "attempt_trigger": "graph_node",
    }


def _parse_metadata_completion(
    response_json: Any, response_text: str | None
) -> dict[str, Any] | None:
    """Parse metadata completion response into a dict (verbatim from legacy)."""
    import json

    candidate: dict[str, Any] | None = None
    if isinstance(response_json, dict):
        choices = response_json.get("choices") or []
        if choices:
            message = (choices[0] or {}).get("message") or {}
            parsed = message.get("parsed")
            if isinstance(parsed, dict):
                candidate = parsed
            elif isinstance(parsed, str):
                try:
                    loaded = json.loads(parsed)
                    if isinstance(loaded, dict):
                        candidate = loaded
                except Exception:
                    candidate = None
            if candidate is None:
                content = message.get("content")
                if isinstance(content, str):
                    candidate = extract_json(content) or None
    if candidate is None and response_text:
        candidate = extract_json(response_text) or None
    return candidate


# ---------------------------------------------------------------------------
# Internal RAG helpers (verbatim from LLMSemanticHelper)
# ---------------------------------------------------------------------------


async def _extract_keywords_tfidf_async(content_text: str, topn: int) -> list[str]:
    if not content_text.strip():
        return []
    try:
        return await asyncio.to_thread(extract_keywords_tfidf, content_text, topn=topn)
    except Exception as exc:  # pragma: no cover - defensive
        raise_if_cancelled(exc)
        logger.warning("tfidf_async_failed", extra={"error": str(exc)})
        return []


def _extract_keywords_simple(text: str, topn: int = 8) -> list[str]:
    if not text or not text.strip():
        return []
    words = re.findall(r"\b\w+\b", text.lower())
    candidates: list[str] = []
    for word in words:
        if len(word) < 4 or word in _SIMPLE_KEYWORD_STOP_WORDS:
            continue
        if word.isdigit():
            continue
        if not any(ch.isalpha() for ch in word):
            continue
        candidates.append(word)
    if not candidates:
        return []
    counts = Counter(candidates)
    return [term for term, _ in counts.most_common(topn)]


async def _generate_query_expansion_keywords(
    summary: dict[str, Any], content_text: str
) -> list[str]:
    seeds: list[str] = []
    for source in ("query_expansion_keywords", "seo_keywords", "key_ideas"):
        values = summary.get(source) or []
        if isinstance(values, list):
            seeds.extend([str(v).strip() for v in values if str(v).strip()])

    topic_tags = summary.get("topic_tags") or []
    seeds.extend([str(t).strip().lstrip("#") for t in topic_tags if str(t).strip()])

    # TF-IDF offloaded to a worker thread (legacy parity, rule 6).
    tfidf_source = (content_text or "")[:20000]
    tfidf_terms = await _extract_keywords_tfidf_async(tfidf_source, topn=40)
    seeds.extend(tfidf_terms)

    deduped: list[str] = []
    seen: set[str] = set()
    for term in seeds:
        key = term.lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(term)

    if len(deduped) < 20:
        for term in tfidf_terms:
            if term not in deduped:
                deduped.append(term)
            if len(deduped) >= 20:
                break

    return deduped[:30]


def _generate_semantic_boosters(base_text: str, summary: dict[str, Any]) -> list[str]:
    boosters: list[str] = []
    from_summary = summary.get("semantic_boosters") or []
    if isinstance(from_summary, list):
        boosters.extend([cap_text(str(b), 320) for b in from_summary if str(b).strip()])

    sentences = re.split(r"(?<=[.!?])\s+", normalize_whitespace(base_text))
    for sentence in sentences:
        if len(boosters) >= 15:
            break
        if sentence and sentence not in boosters and len(sentence) > 20:
            boosters.append(cap_text(sentence, 320))

    return boosters[:15]


def _build_semantic_chunks(
    content_text: str,
    *,
    topics: list[str],
    article_id: str | None,
    language: str | None,
    target_words: int = 150,
) -> list[dict[str, Any]]:
    if not content_text or not content_text.strip():
        return []

    words = content_text.split()
    chunks: list[dict[str, Any]] = []
    start = 0

    while start < len(words):
        end = min(len(words), start + target_words)
        if end - start < 100 and end < len(words):
            end = min(len(words), start + 100)

        chunk_text = " ".join(words[start:end]).strip()
        if not chunk_text:
            break

        local_summary = _extract_local_summary(chunk_text)
        local_keywords = _extract_keywords_simple(chunk_text, topn=8)

        chunks.append(
            {
                "article_id": article_id,
                "section": None,
                "language": language,
                "topics": topics,
                "text": chunk_text,
                "local_summary": local_summary,
                "local_keywords": local_keywords,
            }
        )
        start = end

    return chunks


def _extract_local_summary(chunk_text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", chunk_text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return cap_text(chunk_text, 320)
    selected = " ".join(sentences[:2])
    return cap_text(selected, 320)
