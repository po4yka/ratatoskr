"""CocoIndex flow: summaries table -> Qdrant vector store.

This module defines the incremental ETL flow that keeps Qdrant in sync with
the Postgres `summaries` table. CocoIndex handles watermark tracking,
LISTEN/NOTIFY change detection, and idempotent upserts.

The flow emits ONE point per summary (not chunked windows). This is a
deliberate v1 simplification; chunked points are a follow-up task after
measuring retrieval quality on production queries.

CocoIndex API note: this targets cocoindex>=1.0.3,<1.1. If the installed
version differs, verify the flow_def, fn, sources, and targets APIs
against the installed package's documentation.
"""

from __future__ import annotations

import json
from typing import Any, cast

from app.core.logging_utils import get_logger
from app.infrastructure.cocoindex.embedding_bridge import (
    embed_text_sync,
    repository_id_to_point_id,
    summary_id_to_point_id,
)

logger = get_logger(__name__)


def _coerce_summary_payload(
    json_payload: str | dict[str, Any] | None,
) -> tuple[dict[str, Any], str | None]:
    """Parse a summary's json_payload once.

    Returns ``(payload_dict, raw_fallback)``: ``payload_dict`` is the parsed
    object (``{}`` when absent, non-object, or unparseable), and
    ``raw_fallback`` is the truncated raw string to embed *only* when a string
    payload failed to parse (``None`` otherwise) -- preserving the original
    text-extraction fallback without re-parsing in each helper.
    """
    if not json_payload:
        return {}, None
    if isinstance(json_payload, str):
        try:
            parsed = json.loads(json_payload)
        except (json.JSONDecodeError, ValueError):
            return {}, json_payload[:2000]
        return (parsed if isinstance(parsed, dict) else {}), None
    return (json_payload if isinstance(json_payload, dict) else {}), None


def _extract_indexable_text(payload: dict[str, Any], *, raw_fallback: str | None = None) -> str:
    """Extract the text we embed from a parsed summary payload.

    Mirrors the logic in app.core.embedding_text.prepare_text_for_embedding
    but operates on the parsed payload without the token-length truncation
    (CocoIndex handles batching/chunking at the flow level).
    """
    if not payload:
        return raw_fallback or ""

    parts: list[str] = []
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    title = metadata.get("title") or payload.get("title") or ""
    if title:
        parts.append(title)
    for key in ("summary_1000", "summary_250", "tldr"):
        val = payload.get(key)
        if val and isinstance(val, str):
            parts.append(val)
            break
    key_ideas = payload.get("key_ideas")
    if isinstance(key_ideas, list):
        parts.extend(str(k) for k in key_ideas[:5] if k)
    tags = payload.get("topic_tags")
    if isinstance(tags, list):
        parts.append(" ".join(str(t) for t in tags[:10] if t))
    return " ".join(parts)[:4000]


def _build_qdrant_payload(
    summary_id: int,
    request_id: int,
    lang: str | None,
    payload: dict[str, Any],
    user_scope: str,
    environment: str,
) -> dict[str, Any]:
    """Build the Qdrant point payload dict from a parsed summary payload.

    Must be compatible with the payload schema produced by
    app.infrastructure.vector.metadata_builder.MetadataBuilder so that
    the existing query() path keeps working.
    """
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    return {
        "entity_type": "summary",
        "summary_id": summary_id,
        "request_id": request_id,
        "language": lang or "en",
        "user_scope": user_scope,
        "environment": environment,
        "title": metadata.get("title") or payload.get("title") or "",
        "url": metadata.get("url") or payload.get("url") or "",
        "source_type": payload.get("source_type") or "",
        "tldr": payload.get("tldr") or "",
        "topic_tags": payload.get("topic_tags") or [],
        "summary_250": payload.get("summary_250") or "",
    }


def _parse_json_object(value: str | dict[str, Any] | list[Any] | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_json_list(value: str | list[Any] | None) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _extract_repository_text(row: dict[str, Any], *, max_chars: int = 2000) -> str:
    """Compose repository embedding text from the same weighted signals as the fast path."""
    analysis = _parse_json_object(row.get("analysis_json"))
    languages_json = _parse_json_object(row.get("languages_json"))
    topics = _parse_json_list(row.get("topics_json"))

    parts: list[str] = []
    full_name = row.get("full_name") or ""
    if full_name:
        parts.extend([str(full_name), str(full_name)])

    description = row.get("description")
    if description:
        parts.extend([str(description), str(description)])

    purpose = analysis.get("purpose")
    if purpose:
        parts.append(str(purpose))

    tech_stack = analysis.get("tech_stack")
    if isinstance(tech_stack, list):
        parts.append(" ".join(str(item) for item in tech_stack if item))

    if topics:
        parts.append(" ".join(str(topic) for topic in topics if topic))

    lang_parts: list[str] = []
    primary_language = row.get("primary_language")
    if primary_language:
        lang_parts.append(str(primary_language))
    lang_parts.extend(
        str(lang) for lang in languages_json if lang and str(lang) != str(primary_language)
    )
    if lang_parts:
        parts.append(" ".join(lang_parts))

    architecture = analysis.get("architecture_summary")
    if architecture:
        parts.append(str(architecture)[:500])

    text = " ".join(parts)
    if len(text) >= max_chars:
        return text[:max_chars]

    readme_excerpt = row.get("readme_excerpt")
    if readme_excerpt:
        remaining = max_chars - len(text) - 1
        if remaining > 0:
            text = f"{text} {str(readme_excerpt)[:remaining]}"

    return text[:max_chars]


def _build_repository_payload(
    row: dict[str, Any],
    *,
    user_scope: str,
    environment: str,
) -> dict[str, Any]:
    """Build Qdrant payload compatible with RepositorySearchService filters."""
    topics = _parse_json_list(row.get("topics_json"))
    created_at = row.get("created_at_github")
    created_at_iso = created_at.isoformat() if hasattr(created_at, "isoformat") else created_at
    return {
        "entity_type": "repository",
        "repository_id": row["id"],
        "user_id": row["user_id"],
        "github_id": row["github_id"],
        "full_name": row.get("full_name") or "",
        "primary_language": row.get("primary_language"),
        "topics": [str(topic) for topic in topics if topic],
        "is_starred": bool(row.get("is_starred")),
        "source": row.get("source") or "",
        "created_at": created_at_iso,
        "environment": environment,
        "user_scope": user_scope,
        "language": "en",
    }


# ---------------------------------------------------------------------------
# Flow definition -- uses CocoIndex declarative API
# ---------------------------------------------------------------------------


def build_summaries_flow(
    *,
    collection_name: str,
    qdrant_url: str,
    qdrant_api_key: str | None,
    user_scope: str,
    environment: str,
    listen_channel: str = "ratatoskr_summaries_changed",
) -> Any:
    """Build and return the CocoIndex flow object.

    Called once during startup; the returned flow object is passed to
    CocoIndexRuntime which calls setup() and starts FlowLiveUpdater.

    All CocoIndex imports are lazy so the package is only required when
    cocoindex extra is installed (RATATOSKR_COCOINDEX_ENABLED=1).
    """
    try:
        import cocoindex
    except ImportError as exc:
        raise ImportError(
            "CocoIndex is not installed. Install with: pip install 'cocoindex>=1.0.3,<1.1'"
        ) from exc

    # Capture closure variables for the transform functions
    _user_scope = user_scope
    _environment = environment
    _collection_name = collection_name
    _qdrant_url = qdrant_url
    _qdrant_api_key = qdrant_api_key
    cocoindex_api = cast("Any", cocoindex)
    coco_sources = cocoindex_api.sources
    coco_targets = cocoindex_api.targets

    @cocoindex.flow_def(name="ratatoskr_summaries_to_qdrant")  # type: ignore[attr-defined, untyped-decorator, unused-ignore]
    def summaries_to_qdrant(flow_builder: Any, data_scope: Any) -> None:
        """CocoIndex flow: incrementally sync summaries -> Qdrant."""
        data_scope["summaries"] = flow_builder.add_source(
            coco_sources.Postgres(
                table_name="summaries",
                ordinal_field="updated_at",
                primary_key_fields=["id", "request_id"],
            )
        )

        qdrant_sink = data_scope.add_collector()

        with data_scope["summaries"].row() as row:
            summary_id = row["id"]
            request_id = row["request_id"]
            lang = row.get("lang")
            json_payload = row.get("json_payload")

            # Parse json_payload once and reuse the parsed dict for both the
            # embedding text and the Qdrant payload, instead of json.loads-ing
            # the same blob twice per row.
            payload_dict, raw_fallback = _coerce_summary_payload(json_payload)
            text = _extract_indexable_text(payload_dict, raw_fallback=raw_fallback)
            if not text:
                return

            embedding = embed_text_sync(text, language=lang)
            point_id = summary_id_to_point_id(request_id, summary_id)
            payload = _build_qdrant_payload(
                summary_id=summary_id,
                request_id=request_id,
                lang=lang,
                payload=payload_dict,
                user_scope=_user_scope,
                environment=_environment,
            )

            qdrant_sink.collect(
                id=point_id,
                vector=embedding,
                payload=payload,
            )

        qdrant_sink.export(
            "qdrant_points",
            coco_targets.Qdrant(
                collection_name=_collection_name,
                url=_qdrant_url,
                api_key=_qdrant_api_key,
            ),
            primary_key_fields=["id"],
        )

    return summaries_to_qdrant


def build_repositories_flow(
    *,
    collection_name: str,
    qdrant_url: str,
    qdrant_api_key: str | None,
    user_scope: str,
    environment: str,
) -> Any:
    """Build and return the CocoIndex flow syncing analyzed repositories to Qdrant."""
    try:
        import cocoindex
    except ImportError as exc:
        raise ImportError(
            "CocoIndex is not installed. Install with: pip install 'cocoindex>=1.0.3,<1.1'"
        ) from exc

    _user_scope = user_scope
    _environment = environment
    _collection_name = collection_name
    _qdrant_url = qdrant_url
    _qdrant_api_key = qdrant_api_key
    cocoindex_api = cast("Any", cocoindex)
    coco_sources = cocoindex_api.sources
    coco_targets = cocoindex_api.targets

    @cocoindex.flow_def(name="ratatoskr_repositories_to_qdrant")  # type: ignore[attr-defined, untyped-decorator, unused-ignore]
    def repositories_to_qdrant(flow_builder: Any, data_scope: Any) -> None:
        """CocoIndex flow: incrementally sync analyzed repositories -> Qdrant."""
        data_scope["repositories"] = flow_builder.add_source(
            coco_sources.Postgres(
                table_name="repositories",
                ordinal_field="updated_at",
                primary_key_fields=["id"],
            )
        )

        qdrant_sink = data_scope.add_collector()

        with data_scope["repositories"].row() as row:
            repo_id = row["id"]
            analysis_json = row.get("analysis_json")
            if not analysis_json:
                return

            row_payload = {
                "id": repo_id,
                "github_id": row.get("github_id"),
                "user_id": row.get("user_id"),
                "full_name": row.get("full_name"),
                "description": row.get("description"),
                "primary_language": row.get("primary_language"),
                "languages_json": row.get("languages_json"),
                "topics_json": row.get("topics_json"),
                "readme_excerpt": row.get("readme_excerpt"),
                "analysis_json": analysis_json,
                "is_starred": row.get("is_starred"),
                "source": row.get("source"),
                "created_at_github": row.get("created_at_github"),
            }
            text = _extract_repository_text(row_payload)
            if not text:
                return

            embedding = embed_text_sync(text, language=None)
            point_id = repository_id_to_point_id(_environment, _user_scope, repo_id)
            payload = _build_repository_payload(
                row_payload,
                user_scope=_user_scope,
                environment=_environment,
            )

            qdrant_sink.collect(
                id=point_id,
                vector=embedding,
                payload=payload,
            )

        qdrant_sink.export(
            "qdrant_repository_points",
            coco_targets.Qdrant(
                collection_name=_collection_name,
                url=_qdrant_url,
                api_key=_qdrant_api_key,
            ),
            primary_key_fields=["id"],
        )

    return repositories_to_qdrant
