from __future__ import annotations

import re
from typing import Any

from app.core.logging_utils import get_logger
from app.core.summary_contract_impl.common import SummaryJSON, clean_string_list, is_numeric
from app.core.summary_contract_impl.entities import normalize_entities_field
from app.core.summary_contract_impl.quality_metadata import (
    merge_summary_quality_metadata,
    sync_prompt_injection_quality,
)
from app.core.summary_contract_impl.text_shaping import (
    compute_flesch_reading_ease,
    enrich_tldr_from_payload,
    extract_keywords_tfidf,
    normalize_readability_score,
    summary_fallback_from_supporting_fields,
    tldr_needs_enrichment,
)
from app.core.summary_text_utils import cap_text as _cap_text, hash_tagify as _hash_tagify

logger = get_logger(__name__)


def validate_summary_payload_input(payload: SummaryJSON) -> None:
    if not payload or not isinstance(payload, dict):
        msg = "Summary payload must be a non-empty dictionary"
        raise ValueError(msg)
    if len(str(payload)) > 100000:
        msg = "Summary payload too large"
        raise ValueError(msg)


def backfill_summary_fields(payload: SummaryJSON, original_payload: SummaryJSON) -> None:
    tldr = str(payload.get("tldr", "")).strip()
    summary_250 = str(payload.get("summary_250", "")).strip()
    summary_1000 = str(payload.get("summary_1000", "")).strip()

    if not summary_1000 and "summary" in payload:
        summary_1000 = str(payload.get("summary", "")).strip()
    if not tldr and summary_1000:
        tldr = summary_1000
    if not summary_1000 and tldr:
        summary_1000 = tldr
    if not summary_250 and summary_1000:
        summary_250 = _cap_text(summary_1000, 250)
    if not summary_250 and tldr:
        summary_250 = _cap_text(tldr, 250)

    if not any((summary_250, summary_1000, tldr)):
        fallback_text = summary_fallback_from_supporting_fields(payload)
        if not fallback_text:
            fallback_text = summary_fallback_from_supporting_fields(original_payload)
        if fallback_text:
            summary_1000 = _cap_text(fallback_text, 1000)
            summary_250 = _cap_text(summary_1000, 250)
            tldr = summary_1000

    summary_250 = _cap_text(summary_250, 250)
    summary_1000 = _cap_text(summary_1000, 1000)
    if not summary_1000 and summary_250:
        summary_1000 = summary_250
    if not tldr:
        tldr = summary_1000 or summary_250
    if tldr_needs_enrichment(tldr, summary_1000):
        tldr = enrich_tldr_from_payload(summary_1000 or tldr, payload)

    payload["summary_250"] = summary_250
    payload["summary_1000"] = summary_1000
    payload["tldr"] = tldr


def shape_base_summary_fields(payload: SummaryJSON) -> str:
    payload["key_ideas"] = [
        str(value).strip() for value in payload.get("key_ideas", []) if str(value).strip()
    ]
    payload["topic_tags"] = _hash_tagify([str(value) for value in payload.get("topic_tags", [])])
    payload["entities"] = normalize_entities_field(payload.get("entities"))

    readability = payload.get("readability") or {}
    method = str(readability.get("method") or "Flesch-Kincaid")
    score_value = readability.get("score")
    level = readability.get("level")
    read_src = (
        payload.get("tldr") or payload.get("summary_1000") or payload.get("summary_250") or ""
    )
    score = resolve_readability_score(score_value, str(read_src))
    if score_value is None or not is_numeric(score_value) or float(score_value or 0.0) == 0.0:
        method = "Flesch-Kincaid"
    payload["readability"] = {
        "method": method,
        "score": normalize_readability_score(score),
        "level": level or readability_level(score),
    }
    return str(read_src)


def resolve_readability_score(score_val: Any, read_src: str) -> float:
    if score_val is None or not is_numeric(score_val) or float(score_val or 0.0) == 0.0:
        try:
            return compute_flesch_reading_ease(read_src)
        except Exception:
            try:
                return float(score_val or 0.0)
            except Exception:
                return 0.0
    try:
        return float(score_val or 0.0)
    except Exception:
        return 0.0


def readability_level(score: float) -> str:
    if score >= 90:
        return "Very Easy"
    if score >= 80:
        return "Easy"
    if score >= 70:
        return "Fairly Easy"
    if score >= 60:
        return "Standard"
    if score >= 50:
        return "Fairly Difficult"
    if score >= 30:
        return "Difficult"
    return "Very Confusing"


def populate_keywords_if_missing(payload: SummaryJSON, read_src: str) -> None:
    if payload.get("seo_keywords") and payload.get("topic_tags"):
        return
    terms: list[str] = []
    try:  # pragma: no cover - optional heavy deps
        terms = extract_keywords_tfidf(read_src, topn=10)
    except Exception as exc:
        logger.warning("keyword_extraction_failed", extra={"error": str(exc)})
        terms = []
    if not payload.get("seo_keywords"):
        payload["seo_keywords"] = terms[:10]
    if not payload.get("topic_tags") and terms:
        payload["topic_tags"] = _hash_tagify(terms)


def shape_insights(raw: Any) -> dict[str, Any]:
    shaped: dict[str, Any] = {
        "topic_overview": "",
        "new_facts": [],
        "open_questions": [],
        "suggested_sources": [],
        "expansion_topics": [],
        "next_exploration": [],
        "caution": None,
    }

    if not isinstance(raw, dict):
        return shaped

    shaped["topic_overview"] = str(raw.get("topic_overview", "")).strip()

    facts: list[dict[str, Any]] = []
    seen_facts: set[str] = set()
    for fact in raw.get("new_facts", []) or []:
        if not isinstance(fact, dict):
            continue
        fact_text = str(fact.get("fact", "")).strip()
        if not fact_text:
            continue
        fact_key = fact_text.lower()
        if fact_key in seen_facts:
            continue
        seen_facts.add(fact_key)
        why_value = str(fact.get("why_it_matters", "")).strip() or None
        source_value = str(fact.get("source_hint", "")).strip() or None
        confidence_raw = fact.get("confidence")
        if isinstance(confidence_raw, int | float):
            confidence_value: float | str | None = float(confidence_raw)
        elif confidence_raw is None:
            confidence_value = None
        else:
            confidence_value = str(confidence_raw).strip() or None
        facts.append(
            {
                "fact": fact_text,
                "why_it_matters": why_value,
                "source_hint": source_value,
                "confidence": confidence_value,
            }
        )
    shaped["new_facts"] = facts

    shaped["open_questions"] = clean_string_list(raw.get("open_questions"))
    shaped["suggested_sources"] = clean_string_list(raw.get("suggested_sources"))
    shaped["expansion_topics"] = clean_string_list(raw.get("expansion_topics"))
    shaped["next_exploration"] = clean_string_list(raw.get("next_exploration"))
    shaped["critique"] = clean_string_list(raw.get("critique"))

    caution_raw = raw.get("caution")
    caution_value = str(caution_raw).strip() if caution_raw is not None else ""
    shaped["caution"] = caution_value or None

    return shaped


def shape_quality(raw: Any) -> dict[str, Any]:
    shaped: dict[str, Any] = {
        "author_bias": None,
        "emotional_tone": None,
        "missing_perspectives": [],
        "evidence_quality": None,
        "prompt_injection_suspected": False,
    }

    if not isinstance(raw, dict):
        return shaped

    bias = str(raw.get("author_bias") or "").strip()
    shaped["author_bias"] = bias if bias else None

    tone = str(raw.get("emotional_tone") or "").strip()
    shaped["emotional_tone"] = tone if tone else None

    evidence = str(raw.get("evidence_quality") or "").strip()
    shaped["evidence_quality"] = evidence if evidence else None

    shaped["missing_perspectives"] = clean_string_list(raw.get("missing_perspectives"))
    shaped["prompt_injection_suspected"] = bool(raw.get("prompt_injection_suspected"))

    return shaped


def shape_extended_summary_fields(payload: SummaryJSON) -> None:
    normalize_uncertainty_and_classification(payload)
    payload["insights"] = shape_insights(payload.get("insights"))
    payload["quality"] = shape_quality(payload.get("quality"))
    sync_prompt_injection_quality(payload)
    payload["extractive_quotes"] = [
        {
            "text": str(quote.get("text", "")).strip(),
            "source_span": str(quote.get("source_span", "")).strip() or None,
        }
        for quote in (payload.get("extractive_quotes") or [])
        if isinstance(quote, dict) and str(quote.get("text", "")).strip()
    ]
    payload["highlights"] = [
        str(item).strip() for item in (payload.get("highlights") or []) if str(item).strip()
    ]
    payload["questions_answered"] = shape_questions_answered(
        payload.get("questions_answered") or []
    )
    payload["categories"] = [
        str(category).strip()
        for category in (payload.get("categories") or [])
        if str(category).strip()
    ]
    payload["key_points_to_remember"] = [
        str(item).strip()
        for item in (payload.get("key_points_to_remember") or [])
        if str(item).strip()
    ]
    payload["topic_taxonomy"] = shape_topic_taxonomy(payload.get("topic_taxonomy") or [])


def normalize_uncertainty_and_classification(payload: SummaryJSON) -> None:
    """Normalize optional model-quality metadata without optimistic fallbacks."""
    validation_warnings: list[str] = []
    confidence = payload.get("confidence")
    if confidence is None or str(confidence).strip() == "":
        logger.warning("summary_confidence_missing")
        validation_warnings.append("confidence_missing")
        payload["confidence"] = 0.0
    else:
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            logger.warning("summary_confidence_invalid", extra={"value": str(confidence)})
            validation_warnings.append("confidence_invalid")
            payload["confidence"] = 0.0
        else:
            if confidence_value < 0.0 or confidence_value > 1.0:
                logger.warning("summary_confidence_invalid", extra={"value": str(confidence)})
                validation_warnings.append("confidence_invalid")
            payload["confidence"] = max(0.0, min(1.0, confidence_value))

    risk = payload.get("hallucination_risk")
    if risk is None or str(risk).strip() == "":
        logger.warning("summary_hallucination_risk_missing")
        validation_warnings.append("hallucination_risk_missing")
        payload["hallucination_risk"] = "unknown"
    else:
        risk = getattr(risk, "value", risk)
        risk_value = str(risk).strip().lower()
        if risk_value == "medium":
            risk_value = "med"
        if risk_value not in {"low", "med", "high", "unknown"}:
            logger.warning("summary_hallucination_risk_invalid", extra={"value": str(risk)})
            validation_warnings.append("hallucination_risk_invalid")
            risk_value = "unknown"
        payload["hallucination_risk"] = risk_value

    source_type = payload.get("source_type")
    valid_source_types = {
        "news",
        "blog",
        "research",
        "opinion",
        "tutorial",
        "reference",
        "pdf",
        "unknown",
    }
    if source_type is None or str(source_type).strip() == "":
        payload["source_type"] = "unknown"
    else:
        source_type = getattr(source_type, "value", source_type)
        source_value = str(source_type).strip().lower()
        if source_value not in valid_source_types:
            logger.warning("summary_source_type_invalid", extra={"value": str(source_type)})
            validation_warnings.append("source_type_invalid")
            source_value = "unknown"
        payload["source_type"] = source_value

    freshness = payload.get("temporal_freshness")
    valid_freshness = {"breaking", "recent", "evergreen", "unknown"}
    if freshness is None or str(freshness).strip() == "":
        payload["temporal_freshness"] = "unknown"
    else:
        freshness = getattr(freshness, "value", freshness)
        freshness_value = str(freshness).strip().lower()
        if freshness_value not in valid_freshness:
            logger.warning("summary_temporal_freshness_invalid", extra={"value": str(freshness)})
            validation_warnings.append("temporal_freshness_invalid")
            freshness_value = "unknown"
        payload["temporal_freshness"] = freshness_value

    merge_summary_quality_metadata(payload, validation_warnings=validation_warnings)


def shape_questions_answered(raw_items: list[Any]) -> list[dict[str, str]]:
    clean_qa: list[dict[str, str]] = []
    qa_patterns = [
        r"Q:\s*(.+?)\s*A:\s*(.+)",
        r"Question:\s*(.+?)\s*Answer:\s*(.+)",
        r"(.+?)\?\s*(.+)",
    ]
    for qa in raw_items:
        if isinstance(qa, dict):
            question = str(qa.get("question", "")).strip()
            answer = str(qa.get("answer", "")).strip()
            if question and answer:
                clean_qa.append({"question": question, "answer": answer})
            continue
        if not isinstance(qa, str):
            continue
        qa_str = qa.strip()
        if not qa_str:
            continue
        matched = False
        for pattern in qa_patterns:
            match = re.search(pattern, qa_str, re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            question = match.group(1).strip()
            answer = match.group(2).strip()
            if question and answer:
                clean_qa.append({"question": question, "answer": answer})
                matched = True
                break
        if not matched:
            clean_qa.append({"question": qa_str, "answer": ""})
    return clean_qa


def shape_topic_taxonomy(raw_taxonomy: list[Any]) -> list[dict[str, Any]]:
    clean_taxonomy: list[dict[str, Any]] = []
    for tax in raw_taxonomy:
        if not isinstance(tax, dict) or not str(tax.get("label", "")).strip():
            continue
        clean_taxonomy.append(
            {
                "label": str(tax["label"]).strip(),
                "score": float(tax.get("score", 0.0)) if is_numeric(tax.get("score")) else 0.0,
                "path": str(tax.get("path", "")).strip() or None,
            }
        )
    return clean_taxonomy
