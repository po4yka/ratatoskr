from __future__ import annotations

import importlib
import re
from typing import Any

from app.core.logging_utils import get_logger

try:
    import orjson

    _HAS_ORJSON = True
except Exception:  # pragma: no cover
    import json

    orjson = None
    _HAS_ORJSON = False

LOGGER = get_logger(__name__)


def loads(data: str | bytes) -> Any:
    """Parse JSON string or bytes using orjson if available, else stdlib json.

    Args:
        data: JSON string or bytes to parse

    Returns:
        Parsed Python object
    """
    if _HAS_ORJSON and orjson is not None:
        # orjson.loads accepts both str and bytes
        return orjson.loads(data)
    # stdlib json only accepts str
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return json.loads(data)


def dumps(obj: Any, *, indent: int | None = None, ensure_ascii: bool = False) -> str:
    """Serialize object to JSON string using orjson if available, else stdlib json.

    Args:
        obj: Python object to serialize
        indent: Number of spaces for indentation (None for compact)
        ensure_ascii: Whether to escape non-ASCII characters

    Returns:
        JSON string
    """
    if _HAS_ORJSON and orjson is not None:
        # orjson.dumps returns bytes, so decode to str.
        # orjson never ASCII-escapes unicode by default; no option needed for
        # ensure_ascii=False. OPT_NON_STR_KEYS is unrelated (non-str dict keys)
        # and must not be enabled here.
        options = orjson.OPT_INDENT_2 if indent is not None else 0
        result = orjson.dumps(obj, option=options)
        return result.decode("utf-8")
    return json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii)


def extract_json(text: str) -> dict[str, Any] | None:
    """Extract a JSON object from a string.

    Attempts to recover from common issues such as Markdown code fences,
    extra explanatory text, trailing commas, or missing closing braces without
    making additional LLM calls. Returns the parsed object or ``None`` if
    parsing fails.
    """
    if not isinstance(text, str):
        return None

    candidate = text.strip()
    if not candidate:
        return None

    # Handle markdown-style code fences: ```json ... ```
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", candidate, flags=re.IGNORECASE | re.DOTALL)
    candidate = fence_match.group(1).strip() if fence_match else candidate.strip("`")

    # Remove leading "json" language hint if present
    candidate = re.sub(r"^json\s*", "", candidate, flags=re.IGNORECASE)

    def _try_parse(raw: str) -> dict[str, Any] | None:
        try:
            parsed = loads(raw)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    parsed = _try_parse(candidate)
    if parsed is not None:
        return parsed

    # Attempt to locate the first JSON object within the candidate string
    start = candidate.find("{")
    if start == -1:
        return None
    end = candidate.rfind("}")
    snippet = candidate[start:] if end == -1 or end <= start else candidate[start : end + 1]
    parsed = _try_parse(snippet)
    if parsed is not None:
        return parsed

    # Remove dangling trailing commas
    snippet = re.sub(r",\s*([}\]])", r"\1", snippet)
    parsed = _try_parse(snippet)
    if parsed is not None:
        return parsed

    # Balance braces if the response was truncated near the end
    brace_diff = snippet.count("{") - snippet.count("}")
    if brace_diff > 0:
        snippet = snippet + ("}" * brace_diff)
        parsed = _try_parse(snippet)
        if parsed is not None:
            return parsed

    # Last resort: use json_repair library (lazy import, matching json_validation.py pattern)
    try:
        module = importlib.import_module("json_repair")
        repair_func = getattr(module, "repair_json", None)
        if callable(repair_func):
            repaired = repair_func(candidate)
            if isinstance(repaired, str):
                parsed = _try_parse(repaired.strip())
                if parsed is not None:
                    return parsed
    except Exception as exc:
        LOGGER.debug("json_repair_failed", extra={"error": str(exc)})

    return None
