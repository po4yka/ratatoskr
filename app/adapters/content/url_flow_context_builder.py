"""URL-flow system-prompt loader.

The legacy ``URLFlowContextBuilder`` (extraction + language + map-reduce chunking
context for the pre-graph URL flow) was removed at the T9 graph cutover (audit
#21): it was unreachable from production -- the summarize graph owns extraction,
language detection, and long-context routing, and the only chunking strategy it
exposed (``ContentChunker``) had no live importer. ``get_url_system_prompt``
survives because the pre-extracted background handler still loads the URL
summarization prompt through it.
"""

from __future__ import annotations

from app.core.logging_utils import get_logger
from app.prompts.manager import get_prompt_manager

logger = get_logger(__name__)


def get_url_system_prompt(lang: str) -> str:
    """Load the URL summarization prompt for the chosen language."""
    try:
        manager = get_prompt_manager()
        return manager.get_system_prompt(lang, include_examples=True, num_examples=2)
    except Exception as exc:
        logger.warning(
            "system_prompt_load_failed",
            extra={"lang": lang, "error": str(exc)},
        )
        return (
            "You are a precise assistant that returns only a strict JSON object "
            "matching the provided schema. Output valid UTF-8 JSON only."
        )
