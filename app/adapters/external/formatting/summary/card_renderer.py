"""Compact summary card rendering for Telegram HTML."""

from __future__ import annotations

import html
import re
from typing import Any

from app.core.logging_utils import get_logger
from app.core.ui_strings import t

logger = get_logger(__name__)


def truncate_plain_text(text: str, max_len: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if max_len <= 0 or len(text) <= max_len:
        return text

    soft_min = max(0, int(max_len * 0.7))
    cut = text.rfind(" ", soft_min, max_len)
    if cut == -1:
        cut = max_len
    return text[:cut].rstrip() + "\u2026"


def extract_domain_from_url(url: str) -> str | None:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = (parsed.netloc or "").strip()
        return host or None
    except Exception:
        logger.debug("domain_extraction_failed", exc_info=True)
        return None


def sanitize_tldr(
    text: str,
    *,
    text_processor: Any,
) -> str:
    """Sanitize TLDR text without truncation."""
    sanitized = text_processor.sanitize_summary_text(text) if text else ""
    return re.sub(r"\s+", " ", sanitized).strip()


def build_card_sections(
    summary_shaped: dict[str, Any],
    llm: Any,
    chunks: int | None,
    *,
    reader: bool,
    text_processor: Any,
    data_formatter: Any,
    lang: str = "en",
) -> list[str]:
    """Return logical card sections, each safe for a single Telegram message.

    Sections:
      [0] Header: title + meta + full TLDR (no truncation)
      [1] TLDR RU (only for non-Russian lang when field is present)
      [2] Details: key takeaways + key stats + metadata + model info
    """

    def capped(items: list[str], cap: int, *, sep: str) -> tuple[str, int]:
        clean = [str(x).strip() for x in items if str(x).strip()]
        shown = clean[:cap]
        hidden = max(0, len(clean) - len(shown))
        return (sep.join(shown), hidden) if shown else ("", 0)

    meta = summary_shaped.get("metadata") or {}
    title = str(meta.get("title") or "").strip() if isinstance(meta, dict) else ""
    canonical_url = ""
    domain = ""
    if isinstance(meta, dict):
        canonical_url = str(meta.get("canonical_url") or "").strip()
        domain = str(meta.get("domain") or "").strip()

    if not domain and canonical_url:
        domain = extract_domain_from_url(canonical_url) or ""

    reading_time = summary_shaped.get("estimated_reading_time_min")
    reading_time_str = ""
    try:
        if reading_time is not None:
            reading_time_val = int(reading_time)
            if reading_time_val > 0:
                reading_time_str = f"~{reading_time_val} min"
    except Exception:
        logger.debug("reading_time_conversion_failed", exc_info=True)
        reading_time_str = ""

    display_title = truncate_plain_text(title or domain or t("article", lang), 180)
    if canonical_url:
        title_line = (
            f'<a href="{html.escape(canonical_url, quote=True)}">{html.escape(display_title)}</a>'
        )
    else:
        title_line = html.escape(display_title)

    meta_parts: list[str] = []
    if domain:
        meta_parts.append(html.escape(domain))
    if reading_time_str:
        meta_parts.append(html.escape(reading_time_str))

    source_type = str(summary_shaped.get("source_type") or "").strip().lower()
    freshness = str(summary_shaped.get("temporal_freshness") or "").strip().lower()
    if source_type and source_type != "blog":
        meta_parts.append(html.escape(source_type.capitalize()))
    if freshness and freshness not in ("evergreen", ""):
        meta_parts.append(html.escape(freshness.capitalize()))

    meta_line = " \u00b7 ".join(meta_parts)

    # --- Section 0: Header + TLDR ---
    header_lines: list[str] = [title_line]
    if meta_line:
        header_lines.append(f"<i>{meta_line}</i>")

    tldr_raw = (
        str(summary_shaped.get("tldr") or "").strip()
        or str(summary_shaped.get("summary_250") or "").strip()
    )
    tldr_clean = sanitize_tldr(tldr_raw, text_processor=text_processor)
    if tldr_clean:
        header_lines.extend(["", f"<b>{t('tldr', lang)}</b>", html.escape(tldr_clean)])

    sections: list[str] = ["\n".join(header_lines).strip()]

    # TLDR RU as a separate section (avoids header overflow past 4096-char Telegram limit)
    _cyrillic_chars = sum(1 for ch in tldr_clean if "\u0400" <= ch <= "\u04ff") if tldr_clean else 0
    _tldr_is_russian = _cyrillic_chars > len(tldr_clean) * 0.3 if tldr_clean else False
    tldr_ru_raw = str(summary_shaped.get("tldr_ru") or "").strip()
    if tldr_ru_raw and lang != "ru" and not _tldr_is_russian:
        tldr_ru_clean_ru = sanitize_tldr(tldr_ru_raw, text_processor=text_processor)
        if tldr_ru_clean_ru:
            sections.append(f"<b>{t('tldr_ru', lang)}</b>\n{html.escape(tldr_ru_clean_ru)}")

    # --- Section 2: Details (takeaways + stats + metadata + model) ---
    detail_lines: list[str] = []

    takeaways = summary_shaped.get("key_ideas") or []
    if not isinstance(takeaways, list):
        takeaways = []
    takeaways_clean: list[str] = []
    for item in takeaways:
        s = str(item or "").strip()
        if not s:
            continue
        s = text_processor.sanitize_summary_text(s)
        s = truncate_plain_text(s, 180)
        takeaways_clean.append(html.escape(s))
        if len(takeaways_clean) >= 5:
            break

    if takeaways_clean:
        detail_lines.append(f"<b>{t('key_takeaways', lang)}</b>")
        detail_lines.extend([f"\u2022 {item}" for item in takeaways_clean])

    key_stats = summary_shaped.get("key_stats") or []
    stats_lines: list[str] = []
    if isinstance(key_stats, list) and key_stats:
        stats_lines = data_formatter.format_key_stats_compact(key_stats[:5])

    if stats_lines:
        if detail_lines:
            detail_lines.append("")
        detail_lines.append(f"<b>{t('key_stats', lang)}</b>")
        detail_lines.extend(stats_lines[:5])

    tags_raw = summary_shaped.get("topic_tags") or []
    tags: list[str] = tags_raw if isinstance(tags_raw, list) else []
    tags_shown, tags_hidden = capped(tags, 5, sep=" ")

    entities = summary_shaped.get("entities") or {}
    people: list[str] = []
    orgs: list[str] = []
    places: list[str] = []
    if isinstance(entities, dict):
        people = [str(x).strip() for x in (entities.get("people") or []) if str(x).strip()]
        orgs = [str(x).strip() for x in (entities.get("organizations") or []) if str(x).strip()]
        places = [str(x).strip() for x in (entities.get("locations") or []) if str(x).strip()]

    people_shown, people_hidden = capped(people, 5, sep=", ")
    orgs_shown, orgs_hidden = capped(orgs, 5, sep=", ")
    places_shown, places_hidden = capped(places, 5, sep=", ")

    meta_lines: list[str] = []
    if tags_shown:
        tag_tail = f" (+{tags_hidden})" if tags_hidden else ""
        meta_lines.append(t("tags", lang) + ": " + html.escape(tags_shown + tag_tail))
    if people_shown:
        tail = f" (+{people_hidden})" if people_hidden else ""
        meta_lines.append(t("people", lang) + ": " + html.escape(people_shown + tail))
    if orgs_shown:
        tail = f" (+{orgs_hidden})" if orgs_hidden else ""
        meta_lines.append(t("orgs", lang) + ": " + html.escape(orgs_shown + tail))
    if places_shown:
        tail = f" (+{places_hidden})" if places_hidden else ""
        meta_lines.append(t("places", lang) + ": " + html.escape(places_shown + tail))

    # --- Key interesting facts (from insights.new_facts) ---
    insights = summary_shaped.get("insights") or {}
    if isinstance(insights, dict):
        new_facts_raw = insights.get("new_facts") or []
        if isinstance(new_facts_raw, list):
            facts_clean: list[str] = []
            for fact_entry in new_facts_raw:
                fact_text = ""
                if isinstance(fact_entry, dict):
                    fact_text = str(fact_entry.get("fact") or "").strip()
                elif isinstance(fact_entry, str):
                    fact_text = fact_entry.strip()
                if not fact_text:
                    continue
                fact_text = text_processor.sanitize_summary_text(fact_text)
                facts_clean.append(html.escape(truncate_plain_text(fact_text, 200)))
                if len(facts_clean) >= 3:
                    break
            if facts_clean:
                if detail_lines:
                    detail_lines.append("")
                detail_lines.append(f"<b>{t('interesting_facts', lang)}</b>")
                detail_lines.extend([f"\u2022 {f}" for f in facts_clean])

        # --- Questionable statements (insights.caution + insights.critique) ---
        caution = str(insights.get("caution") or "").strip()
        critique_raw = insights.get("critique") or []
        critique_items: list[str] = []
        if isinstance(critique_raw, list):
            for c in critique_raw[:3]:
                cs = str(c or "").strip()
                if cs:
                    cs = text_processor.sanitize_summary_text(cs)
                    critique_items.append(html.escape(truncate_plain_text(cs, 200)))

        if caution or critique_items:
            if detail_lines:
                detail_lines.append("")
            detail_lines.append(f"<b>{t('questionable', lang)}</b>")
            if caution:
                caution_clean = text_processor.sanitize_summary_text(caution)
                detail_lines.append(html.escape(truncate_plain_text(caution_clean, 300)))
            detail_lines.extend([f"\u2022 {c}" for c in critique_items])

    if meta_lines:
        if detail_lines:
            detail_lines.append("")
        detail_lines.append(f"<b>{t('metadata', lang)}</b>")
        detail_lines.extend(meta_lines)

    if not reader:
        method = f"{t('chunked', lang)} ({chunks} parts)" if chunks else t("single_pass", lang)
        model_name = getattr(llm, "model", None) or "unknown"
        model_parts = [
            f"{t('model', lang)}: {html.escape(str(model_name))}",
            html.escape(method),
        ]

        confidence = summary_shaped.get("confidence")
        try:
            conf_val = float(confidence) if confidence is not None else 0.0
        except (ValueError, TypeError):
            conf_val = 0.0
        if conf_val < 1.0:
            model_parts.append(f"{int(conf_val * 100)}%")

        h_risk = str(summary_shaped.get("hallucination_risk") or "").strip().lower()
        if h_risk and h_risk != "low":
            model_parts.append(f"{t('hallucination_risk', lang)}: {html.escape(h_risk)}")

        if detail_lines:
            detail_lines.append("")
        model_info = " \u00b7 ".join(model_parts)
        detail_lines.append(f"<i>{model_info}</i>")

    if detail_lines:
        sections.append("\n".join(detail_lines).strip())

    return sections


def build_compact_card_html(
    summary_shaped: dict[str, Any],
    llm: Any,
    chunks: int | None,
    *,
    reader: bool,
    text_processor: Any,
    data_formatter: Any,
    lang: str = "en",
) -> str:
    """Build a single HTML string from all card sections (for crosspost / fallback)."""
    sections = build_card_sections(
        summary_shaped,
        llm,
        chunks,
        reader=reader,
        text_processor=text_processor,
        data_formatter=data_formatter,
        lang=lang,
    )
    return "\n\n".join(sections).strip() or f"\u2705 {t('summary_ready', lang)}"
