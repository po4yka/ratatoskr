"""Incremental section assembler for streamed summary JSON tokens."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, cast

from app.core.json_utils import extract_json

_SECTION_ORDER = ("summary_250", "tldr", "key_ideas", "topic_tags")

_STRING_FIELD_PATTERNS = {
    "summary_250": re.compile(r'"summary_250"\s*:\s*"((?:[^"\\]|\\.)*)"', re.DOTALL),
    "tldr": re.compile(r'"tldr"\s*:\s*"((?:[^"\\]|\\.)*)"', re.DOTALL),
}

_ARRAY_FIELD_PATTERNS = {
    "key_ideas": re.compile(r'"key_ideas"\s*:\s*\[(.*?)\]', re.DOTALL),
    "topic_tags": re.compile(r'"topic_tags"\s*:\s*\[(.*?)\]', re.DOTALL),
}


@dataclass(frozen=True)
class SummarySectionSnapshot:
    section: str
    value: str | list[str]


class SummarySectionStreamAssembler:
    """Converts streamed token deltas into ordered summary section snapshots."""

    def __init__(self) -> None:
        self._buffer_parts: list[str] = []
        self._sections: dict[str, str | list[str]] = {}

    @property
    def sections(self) -> dict[str, str | list[str]]:
        return dict(self._sections)

    def add_delta(self, delta: str) -> list[SummarySectionSnapshot]:
        if not delta:
            return []

        self._buffer_parts.append(delta)
        # A section can only become complete at a JSON string/array/object
        # boundary. Avoid rescanning the growing payload for ordinary text
        # tokens; doing that for every delta is quadratic in response size.
        if not any(boundary in delta for boundary in ('"', "]", "}")):
            return []

        parsed = self._extract_sections("".join(self._buffer_parts))
        emitted: list[SummarySectionSnapshot] = []

        for section in _SECTION_ORDER:
            value = parsed.get(section)
            if not self._is_meaningful_value(value):
                continue
            if self._sections.get(section) == value:
                continue
            self._sections[section] = value
            emitted.append(SummarySectionSnapshot(section=section, value=value))

        return emitted

    def render_preview(self, *, finalizing: bool = False) -> str:
        lines = ["⏳ Summary is being generated..."]

        summary_250 = self._sections.get("summary_250")
        if isinstance(summary_250, str) and summary_250.strip():
            lines.extend(["", "Summary:", summary_250.strip()])

        tldr = self._sections.get("tldr")
        if isinstance(tldr, str) and tldr.strip():
            lines.extend(["", "TL;DR:", tldr.strip()])

        key_ideas = self._sections.get("key_ideas")
        if isinstance(key_ideas, list) and key_ideas:
            lines.extend(["", "Key ideas:"])
            lines.extend(f"- {item}" for item in key_ideas[:5])

        topic_tags = self._sections.get("topic_tags")
        if isinstance(topic_tags, list) and topic_tags:
            tag_line = " ".join(f"#{tag.lstrip('#')}" for tag in topic_tags[:8])
            lines.extend(["", f"Tags: {tag_line}"])

        if finalizing:
            lines.extend(["", "Finalizing output..."])

        return "\n".join(lines)

    def _extract_sections(self, raw_text: str) -> dict[str, str | list[str]]:
        # 1) Fast path: complete JSON extraction from accumulated text.
        result: dict[str, str | list[str]] = {}
        fast = self._extract_from_json_object(raw_text)
        if fast:
            result.update(fast)

        # 2) Tolerant partial extraction for still-incomplete JSON payloads.
        tolerant = self._extract_tolerant(raw_text)
        for key, value in tolerant.items():
            result.setdefault(key, value)

        return result

    def _extract_from_json_object(self, raw_text: str) -> dict[str, str | list[str]]:
        try:
            obj = extract_json(raw_text)
        except Exception:
            return {}
        if not isinstance(obj, dict):
            return {}

        extracted: dict[str, str | list[str]] = {}
        for key in _SECTION_ORDER:
            value = obj.get(key)
            if key in ("summary_250", "tldr") and isinstance(value, str):
                cleaned = value.strip()
                if cleaned:
                    extracted[key] = cleaned
            elif key in ("key_ideas", "topic_tags") and isinstance(value, list):
                cleaned_list = [str(item).strip() for item in value if str(item).strip()]
                if cleaned_list:
                    extracted[key] = cleaned_list
        return extracted

    def _extract_tolerant(self, raw_text: str) -> dict[str, str | list[str]]:
        extracted: dict[str, str | list[str]] = {}

        for key, pattern in _STRING_FIELD_PATTERNS.items():
            match = pattern.search(raw_text)
            if not match:
                continue
            value = self._decode_json_string(match.group(1)).strip()
            if value:
                extracted[key] = value

        for key, pattern in _ARRAY_FIELD_PATTERNS.items():
            match = pattern.search(raw_text)
            if match:
                parsed = self._parse_array_inner(match.group(1))
                if parsed:
                    extracted[key] = parsed
                    continue

            # Incomplete array fallback: extract quoted items after key marker.
            marker_idx = raw_text.find(f'"{key}"')
            if marker_idx >= 0:
                tail = raw_text[marker_idx:]
                quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', tail)
                # The first quoted item is usually the key itself.
                values = [self._decode_json_string(item).strip() for item in quoted[1:]]
                values = [item for item in values if item]
                if values:
                    extracted[key] = values[:8]

        return extracted

    def _parse_array_inner(self, raw_inner: str) -> list[str]:
        raw = f"[{raw_inner}]"
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item).strip() for item in parsed if str(item).strip()]

    @staticmethod
    def _decode_json_string(value: str) -> str:
        try:
            return cast("str", json.loads(f'"{value}"'))
        except Exception:
            return value

    @staticmethod
    def _is_meaningful_value(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return any(str(item).strip() for item in value)
        return False
