"""``extract`` node -- fetch content via the extraction port (ADR-0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.deps import SummarizeConfig
from app.application.graphs.summarize.nodes._span import graph_node
from app.application.ports.extraction import ExtractionRequest
from app.core.lang import choose_language

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("extract")
async def extract(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Fetch source content through ``deps.extraction`` (one port that dispatches
    by URL pattern to the scraper chain / youtube / twitter / academic / github /
    meta internally) and project the result into minimal id-based state.

    Only ids/handles + the (single) source-text field are written: the bulk
    ``content_text`` lands in ``state['source_text']`` (consumed by ground +
    summarize); ``dedupe_hash`` / ``content_source`` / ``detected_lang`` /
    ``title`` are small primitives. An extraction failure raises ``ValueError``
    (already persisted via ``persist_request_failure`` inside the adapter), which
    the runner routes to the single terminal-failure path (ADR-0011) -- there is
    NO parallel error path here.
    """
    request_id = state.get("request_id")
    url = (state.get("input_url") or "").strip()
    if not url:
        # No URL settled (e.g. a forwarded message with no link): nothing to
        # fetch. Leave state untouched; downstream nodes see empty source_text.
        return {}

    result = await deps.extraction.extract(
        ExtractionRequest(
            url=url,
            request_id=request_id,
            correlation_id=state.get("correlation_id"),
        )
    )

    # Promote the chosen output language now that the content's language is known.
    # Under the shipped ``preferred_lang: auto`` the detected language wins, so
    # non-English content is summarized/cached/persisted in its own language --
    # downstream nodes (ground/build_prompt/summarize) and the cache key all read
    # ``state['lang']``, which is otherwise still the pre-extraction default ``en``.
    # A forced ``en``/``ru`` preference still pins the output (choose_language
    # returns the preference verbatim when it is en/ru).
    config = deps.config if isinstance(deps.config, SummarizeConfig) else None
    preferred = config.preferred_lang if config is not None else (state.get("lang") or "auto")
    lang = choose_language(preferred, result.detected_lang)

    return {
        "lang": lang,
        "source_text": result.content_text,
        "content_source": result.content_source,
        "detected_lang": result.detected_lang,
        "dedupe_hash": result.dedupe_hash,
        "title": result.title or "",
        # Article-vision (audit #2): carry the extracted image URLs so build_prompt
        # can route image-rich content to the vision model. A serializable list[str].
        "images": list(result.images or []),
    }
