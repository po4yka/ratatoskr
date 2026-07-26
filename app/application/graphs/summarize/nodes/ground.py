"""``ground`` node -- optional RAG grounding via the retrieval port (ADR-0005/0012)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.application.dto.vector_search import EntityType, RetrievalScope
from app.application.graphs.summarize.nodes._span import graph_node
from app.application.graphs.summarize.nodes._context import load_source_text
from app.core.content_cleaner import neutralize_literal_delimiters

if TYPE_CHECKING:
    from app.application.dto.vector_search import RetrievalHit
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState

# Anti-contamination delimiter. The same "RELATED PRIOR SUMMARIES (reference
# only)" phrase appears as static guidance in all four prompt files so the model
# knows how to treat the injected block; an en+ru lockstep test asserts that.
# Keep this header and the prompt-file wording in sync.
GROUNDING_BLOCK_HEADER = "=== RELATED PRIOR SUMMARIES (reference only) ==="
GROUNDING_BLOCK_FOOTER = "=== END RELATED PRIOR SUMMARIES ==="
_GROUNDING_GUARD = (
    "Reference only -- do NOT summarize these, and do NOT introduce facts or "
    "cross-references absent from the source being summarized."
)
# Bound each snippet so the block stays small regardless of summary length.
_SNIPPET_MAX_CHARS = 280
_TITLE_MAX_CHARS = 200
# Strip control chars (newlines/tabs included) so a poisoned stored title/tldr
# cannot forge the block's line structure or inject an instruction on its own
# line (second-order prompt injection, HIGH-010).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _sanitize_grounding_text(value: str, *, max_chars: int) -> str:
    cleaned = _CONTROL_CHARS_RE.sub(" ", value)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    # A poisoned stored title/tldr can also contain the header/footer text
    # verbatim, forging the block's boundary (second-order boundary injection).
    cleaned = neutralize_literal_delimiters(
        cleaned, (GROUNDING_BLOCK_HEADER, GROUNDING_BLOCK_FOOTER)
    )
    return cleaned[:max_chars]


@graph_node("ground")
async def ground(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Retrieve top-k scope-filtered prior summaries and format the grounding block.

    No-op (empty grounding -- byte-identical to the no-RAG path) when
    ``SUMMARIZE_RAG_ENABLED`` is off, the source text is missing, or the scope is
    incomplete. Otherwise embeds the extracted text via the unified retrieval port
    (ADR-0016), EXCLUDES the current ``request_id`` so a re-summarization never
    grounds on its own prior summary, and writes an anti-contamination block for
    ``build_prompt`` to concatenate. Imports only the retrieval port + its DTOs.
    """
    source_text = await load_source_text(state, deps)
    empty: dict[str, Any] = {"grounding_ids": [], "grounding_block": ""}
    if source_text and not state.get("source_text"):
        empty["source_text"] = source_text
    if not deps.rag_enabled:
        return empty

    user_scope = state.get("user_scope")
    environment = state.get("environment")
    if not source_text or not user_scope or not environment:
        # Scope/content are wired by ingest/extract (T7); until then -- or for a
        # request that genuinely lacks them -- stay a no-op rather than issue an
        # unscoped query (the centralized filter's IDOR guard requires scope).
        return empty

    # Summaries are owner-wide at the vector layer: the shared point shape carries
    # no user_id, so user_scope + environment ARE the partition. Passing user_id
    # would filter on a payload field that does not exist -> zero hits. The
    # Postgres-side IDOR re-filter (CLAUDE.md rule 12) guards hydration paths, not
    # this owner-wide summary query (mirrors the legacy StoreVectorSearchService).
    scope = RetrievalScope(
        environment=environment,
        user_scope=user_scope,
        user_id=None,
    )
    retrieval_result = await deps.retrieval.retrieve(
        entity_type=EntityType.SUMMARY,
        scope=scope,
        query=source_text[: deps.rag_query_max_chars],
        top_k=max(1, deps.rag_top_k),
        exclude_request_id=state.get("request_id"),
        correlation_id=state.get("correlation_id"),
    )
    if not retrieval_result.hits:
        return empty

    result: dict[str, Any] = {
        "grounding_ids": [hit.entity_id for hit in retrieval_result.hits],
        "grounding_block": _format_grounding_block(retrieval_result.hits),
    }
    if source_text and not state.get("source_text"):
        result["source_text"] = source_text
    return result


def _format_grounding_block(hits: list[RetrievalHit]) -> str:
    """Format hits as title + tldr/summary_250 snippets (no raw source)."""
    lines = [GROUNDING_BLOCK_HEADER, _GROUNDING_GUARD]
    for index, hit in enumerate(hits, start=1):
        payload = hit.payload or {}
        title = (
            _sanitize_grounding_text(str(payload.get("title") or ""), max_chars=_TITLE_MAX_CHARS)
            or "(untitled)"
        )
        snippet = _sanitize_grounding_text(
            str(payload.get("tldr") or payload.get("summary_250") or ""),
            max_chars=_SNIPPET_MAX_CHARS,
        )
        lines.append(f"{index}. {title}" if not snippet else f"{index}. {title} -- {snippet}")
    lines.append(GROUNDING_BLOCK_FOOTER)
    return "\n".join(lines)
