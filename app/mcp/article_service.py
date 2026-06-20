from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import selectinload

from app.core.logging_utils import generate_correlation_id
from app.mcp.helpers import (
    McpErrorResult,
    ensure_mapping,
    format_summary_compact,
    format_summary_detail,
    isotime,
    paginated_payload,
)

logger = logging.getLogger("ratatoskr.mcp")

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.sql.elements import ColumnElement

    from app.mcp.context import McpServerContext


class ArticleReadService:
    def __init__(self, context: McpServerContext) -> None:
        self.context = context

    async def search_articles(self, query: str, limit: int = 10) -> dict[str, Any] | McpErrorResult:
        """Search stored article summaries by keyword, topic, or entity."""
        from app.db.models import Request, Summary, TopicSearchIndex

        limit = max(1, min(25, limit))
        query_text = query.strip()
        if not query_text:
            return {"results": [], "total": 0, "query": query}

        try:
            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                ts_query = func.websearch_to_tsquery("simple", query_text)
                rank = func.ts_rank_cd(TopicSearchIndex.body_tsv, ts_query)
                rows = (
                    await session.execute(
                        select(TopicSearchIndex.request_id, rank.label("rank"))
                        .join(Request, TopicSearchIndex.request_id == Request.id)
                        .where(
                            TopicSearchIndex.body_tsv.op("@@")(ts_query),
                            *self.context.request_scope_filters(Request),
                        )
                        .order_by(rank.desc(), TopicSearchIndex.request_id.desc())
                        .limit(limit)
                    )
                ).all()

                ranked_request_ids = [int(row.request_id) for row in rows]
                if not ranked_request_ids:
                    return await self._fallback_search(query_text, limit)

                rank_position = {
                    request_id: idx for idx, request_id in enumerate(ranked_request_ids)
                }
                summaries = (
                    await session.scalars(
                        self._summary_stmt(Request, Summary).where(
                            Request.id.in_(ranked_request_ids)
                        )
                    )
                ).all()

            by_request_id: dict[int, dict[str, Any]] = {}
            for summary in summaries:
                request = summary.request
                request_id = int(request.id)
                if request_id not in by_request_id:
                    by_request_id[request_id] = format_summary_compact(summary, request)

            ordered_request_ids = sorted(
                by_request_id.keys(),
                key=lambda request_id: rank_position.get(request_id, len(rank_position)),
            )
            results = [by_request_id[request_id] for request_id in ordered_request_ids][:limit]
            return {"results": results, "total": len(results), "query": query}
        except Exception:
            cid = generate_correlation_id()
            logger.exception("search_articles failed", extra={"correlation_id": cid})
            return {"error": f"Search failed. Error ID: {cid}", "query": query}

    async def _fallback_search(self, query: str, limit: int) -> dict[str, Any]:
        from app.db.models import Request, Summary

        terms = query.lower().split()
        if not terms:
            return {"results": [], "total": 0, "query": query}

        runtime = self.context.ensure_runtime()
        async with runtime.database.session() as session:
            all_summaries = (
                await session.scalars(
                    self._summary_stmt(Request, Summary)
                    .order_by(Summary.created_at.desc())
                    .limit(200)
                )
            ).all()

        results = []
        for summary in all_summaries:
            payload = ensure_mapping(getattr(summary, "json_payload", None))
            searchable = " ".join(
                [
                    str(payload.get("summary_250", "")),
                    str(payload.get("tldr", "")),
                    " ".join(payload.get("topic_tags", [])),
                    " ".join(payload.get("seo_keywords", [])),
                    str(ensure_mapping(payload.get("metadata")).get("title", "")),
                ]
            ).lower()
            if any(term in searchable for term in terms):
                results.append(format_summary_compact(summary, summary.request))
                if len(results) >= limit:
                    break

        return {"results": results, "total": len(results), "query": query}

    async def get_article(self, summary_id: int) -> dict[str, Any] | McpErrorResult:
        from app.db.models import Request, Summary

        try:
            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                summary = await session.scalar(
                    self._summary_stmt(Request, Summary).where(Summary.id == summary_id)
                )
            if summary is None:
                return {"error": f"Summary {summary_id} not found"}
            return format_summary_detail(summary, summary.request)
        except Exception:
            cid = generate_correlation_id()
            logger.exception("get_article failed", extra={"correlation_id": cid})
            return {"error": f"Failed to retrieve article. Error ID: {cid}"}

    async def list_articles(
        self,
        limit: int = 20,
        offset: int = 0,
        is_favorited: bool | None = None,
        lang: str | None = None,
        tag: str | None = None,
    ) -> dict[str, Any]:
        from app.db.models import Request, Summary

        limit = max(1, min(100, limit))
        offset = max(0, offset)

        try:
            filters: list[ColumnElement[bool]] = []
            if is_favorited is not None:
                filters.append(Summary.is_favorited == is_favorited)
            if lang:
                filters.append(Summary.lang == lang)

            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                base_stmt = self._summary_stmt(Request, Summary).where(*filters)

                if tag:
                    # Normalise tag to "#foo" form (all tags are stored hash-prefixed and
                    # lower-cased by summary_shaper._hash_tagify).
                    tag_lower = (tag if tag.startswith("#") else f"#{tag}").lower()
                    # Push the containment check into Postgres via the denormalised
                    # Summary.topic_tags JSONB column (mirrors json_payload.topic_tags).
                    # JSONB @> operator: column must contain the single-element array.
                    tag_filter = Summary.topic_tags.op("@>")(cast([tag_lower], JSONB))
                    tag_stmt = base_stmt.where(tag_filter)

                    total = await session.scalar(
                        select(func.count())
                        .select_from(Summary)
                        .join(Request, Summary.request_id == Request.id)
                        .where(
                            Summary.is_deleted.is_(False),
                            *self.context.request_scope_filters(Request),
                            *filters,
                            tag_filter,
                        )
                    )
                    articles = (
                        await session.scalars(
                            tag_stmt.order_by(Summary.created_at.desc()).offset(offset).limit(limit)
                        )
                    ).all()
                    results = [
                        format_summary_compact(summary, summary.request) for summary in articles
                    ]
                else:
                    total = await session.scalar(
                        select(func.count())
                        .select_from(Summary)
                        .join(Request, Summary.request_id == Request.id)
                        .where(
                            Summary.is_deleted.is_(False),
                            *self.context.request_scope_filters(Request),
                            *filters,
                        )
                    )
                    articles = (
                        await session.scalars(
                            base_stmt.order_by(Summary.created_at.desc())
                            .offset(offset)
                            .limit(limit)
                        )
                    ).all()
                    results = [
                        format_summary_compact(summary, summary.request) for summary in articles
                    ]

            total = total or 0
            payload = paginated_payload(results=results, total=total, limit=limit, offset=offset)
            payload["articles"] = results
            return payload
        except Exception:
            cid = generate_correlation_id()
            logger.exception("list_articles failed", extra={"correlation_id": cid})
            return {"error": f"Failed to list articles. Error ID: {cid}"}

    async def get_article_content(self, summary_id: int) -> dict[str, Any]:
        from app.db.models import CrawlResult, Request, Summary

        try:
            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                summary = await session.scalar(
                    self._summary_stmt(Request, Summary).where(Summary.id == summary_id)
                )
                if summary is None:
                    return {"error": f"Summary {summary_id} not found"}

                request = summary.request
                crawl = await session.scalar(
                    select(CrawlResult).where(
                        CrawlResult.request_id == request.id,
                        CrawlResult.is_deleted.is_(False),
                    )
                )
                if not crawl:
                    return {"error": f"No crawl content found for summary {summary_id}"}

                content = crawl.content_markdown or crawl.content_html or request.content_text or ""
                metadata = ensure_mapping(crawl.metadata_json)
                return {
                    "summary_id": summary_id,
                    "url": getattr(request, "input_url", ""),
                    "title": metadata.get("title", "Untitled"),
                    "content_format": "markdown" if crawl.content_markdown else "text",
                    "content": content[:50000],
                    "content_length": len(content),
                    "truncated": len(content) > 50000,
                }
        except Exception:
            cid = generate_correlation_id()
            logger.exception("get_article_content failed", extra={"correlation_id": cid})
            return {"error": f"Failed to retrieve article content. Error ID: {cid}"}

    async def get_stats(self) -> dict[str, Any]:
        from app.db.models import Request, Summary

        try:
            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                total = await self._summary_count(session, Request, Summary)
                unread = await self._summary_count(
                    session, Request, Summary, [Summary.is_read.is_(False)]
                )
                favorited = await self._summary_count(
                    session, Request, Summary, [Summary.is_favorited.is_(True)]
                )

                lang_rows = await session.execute(
                    select(Summary.lang, func.count().label("count"))
                    .join(Request, Summary.request_id == Request.id)
                    .where(
                        Summary.is_deleted.is_(False),
                        *self.context.request_scope_filters(Request),
                    )
                    .group_by(Summary.lang)
                )
                recent = (
                    await session.scalars(
                        self._summary_stmt(Request, Summary)
                        .order_by(Summary.created_at.desc())
                        .limit(200)
                    )
                ).all()
                url_count = await self._request_count(session, Request, "url")
                forward_count = await self._request_count(session, Request, "forward")

            tag_counts: dict[str, int] = {}
            for summary in recent:
                payload = ensure_mapping(getattr(summary, "json_payload", None))
                for tag in payload.get("topic_tags", []):
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

            top_tags = sorted(tag_counts.items(), key=lambda item: item[1], reverse=True)[:20]
            languages = {(lang or "unknown"): int(count) for lang, count in lang_rows}
            return {
                "total_articles": total,
                "unread": unread,
                "favorited": favorited,
                "languages": languages,
                "top_tags": [{"tag": tag, "count": count} for tag, count in top_tags],
                "request_types": {"url": url_count, "forward": forward_count},
            }
        except Exception:
            cid = generate_correlation_id()
            logger.exception("get_stats failed", extra={"correlation_id": cid})
            return {"error": f"Failed to retrieve stats. Error ID: {cid}"}

    # Maximum number of summaries scanned in-process for entity/payload searches.
    # Prevents unbounded memory growth; a truncation flag is returned when reached.
    _ENTITY_SCAN_LIMIT = 5000

    async def find_by_entity(
        self,
        entity_name: str,
        entity_type: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        from app.db.models import Request, Summary

        limit = max(1, min(25, limit))
        name_lower = entity_name.lower()

        try:
            runtime = self.context.ensure_runtime()
            # Fetch only the columns needed for matching + compact formatting.
            # Capped at _ENTITY_SCAN_LIMIT to bound memory; truncated flag signals
            # that results beyond the cap may be missing.
            async with runtime.database.session() as session:
                rows = (
                    await session.execute(
                        select(Summary.id, Summary.json_payload, Summary.created_at)
                        .join(Request, Summary.request_id == Request.id)
                        .where(
                            Summary.is_deleted.is_(False),
                            *self.context.request_scope_filters(Request),
                        )
                        .order_by(Summary.created_at.desc())
                        .limit(self._ENTITY_SCAN_LIMIT)
                    )
                ).all()
                scanned = len(rows)
                truncated = scanned == self._ENTITY_SCAN_LIMIT

                # Resolve full ORM objects only for matched rows (up to limit).
                matched_ids: list[int] = []
                for row in rows:
                    if len(matched_ids) >= limit:
                        break
                    payload = ensure_mapping(row.json_payload)
                    entities = ensure_mapping(payload.get("entities"))
                    types_to_check = (
                        [entity_type]
                        if entity_type in ("people", "organizations", "locations")
                        else ["people", "organizations", "locations"]
                    )
                    matched = False
                    for entity_kind in types_to_check:
                        for item in entities.get(entity_kind, []):
                            if name_lower in str(item).lower():
                                matched = True
                                break
                        if matched:
                            break
                    if matched:
                        matched_ids.append(int(row.id))

                if not matched_ids:
                    return {
                        "results": [],
                        "total": 0,
                        "entity": entity_name,
                        "entity_type": entity_type,
                        "truncated": truncated,
                    }

                full_summaries = (
                    await session.scalars(
                        self._summary_stmt(Request, Summary).where(Summary.id.in_(matched_ids))
                    )
                ).all()

            by_id = {int(s.id): s for s in full_summaries}
            results = [
                format_summary_compact(by_id[mid], by_id[mid].request)
                for mid in matched_ids
                if mid in by_id
            ]
            return {
                "results": results,
                "total": len(results),
                "entity": entity_name,
                "entity_type": entity_type,
                "truncated": truncated,
            }
        except Exception:
            cid = generate_correlation_id()
            logger.exception("find_by_entity failed", extra={"correlation_id": cid})
            return {"error": f"Entity search failed. Error ID: {cid}"}

    async def check_url(self, url: str) -> dict[str, Any]:
        from app.core.url_utils import compute_dedupe_hash, normalize_url
        from app.db.models import Request, Summary

        try:
            normalized = normalize_url(url)
            dedupe_hash = compute_dedupe_hash(url)
            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                request = await session.scalar(
                    select(Request).where(
                        Request.dedupe_hash == dedupe_hash,
                        *self.context.request_scope_filters(Request),
                    )
                )
                if not request:
                    return {
                        "exists": False,
                        "normalized_url": normalized,
                        "dedupe_hash": dedupe_hash,
                        "message": "URL has not been processed yet",
                    }

                summary = await session.scalar(
                    select(Summary).where(
                        Summary.request_id == request.id,
                        Summary.is_deleted.is_(False),
                    )
                )

            result: dict[str, Any] = {
                "exists": True,
                "normalized_url": normalized,
                "dedupe_hash": dedupe_hash,
                "request_id": request.id,
                "request_status": request.status,
                "request_type": request.type,
                "created_at": isotime(request.created_at),
            }

            if summary:
                result["summary_id"] = summary.id
                result["summary"] = format_summary_compact(summary, request)
            else:
                result["summary_id"] = None
                result["message"] = "URL was processed but no summary is available"
            return result
        except Exception:
            cid = generate_correlation_id()
            logger.exception("check_url failed", extra={"correlation_id": cid})
            return {"error": f"URL check failed. Error ID: {cid}", "url": url}

    async def unread_articles(self, limit: int = 20) -> dict[str, Any]:
        from app.db.models import Request, Summary

        try:
            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                summaries = (
                    await session.scalars(
                        self._summary_stmt(Request, Summary)
                        .where(Summary.is_read.is_(False))
                        .order_by(Summary.created_at.desc())
                        .limit(limit)
                    )
                ).all()
            results = [format_summary_compact(summary, summary.request) for summary in summaries]
            return {"articles": results, "total": len(results)}
        except Exception:
            cid = generate_correlation_id()
            logger.exception("unread_resource failed", extra={"correlation_id": cid})
            return {"error": f"Failed to retrieve unread articles. Error ID: {cid}"}

    async def tag_counts(self) -> dict[str, Any]:
        try:
            counts = await self._aggregate_payload_values("topic_tags")
            sorted_tags = sorted(counts.items(), key=lambda item: item[1], reverse=True)
            return {
                "tags": [{"tag": tag, "count": count} for tag, count in sorted_tags],
                "total_unique_tags": len(sorted_tags),
            }
        except Exception:
            cid = generate_correlation_id()
            logger.exception("tags_resource failed", extra={"correlation_id": cid})
            return {"error": f"Failed to retrieve tag counts. Error ID: {cid}"}

    async def entity_counts(self) -> dict[str, Any]:
        try:
            people: dict[str, int] = {}
            organizations: dict[str, int] = {}
            locations: dict[str, int] = {}

            for payload in await self._summary_payloads():
                entities = ensure_mapping(payload.get("entities"))
                for item in entities.get("people", []):
                    people[item] = people.get(item, 0) + 1
                for item in entities.get("organizations", []):
                    organizations[item] = organizations.get(item, 0) + 1
                for item in entities.get("locations", []):
                    locations[item] = locations.get(item, 0) + 1

            def _top(items: dict[str, int], limit: int = 50) -> list[dict[str, Any]]:
                return [
                    {"name": name, "count": count}
                    for name, count in sorted(
                        items.items(), key=lambda item: item[1], reverse=True
                    )[:limit]
                ]

            return {
                "people": _top(people),
                "organizations": _top(organizations),
                "locations": _top(locations),
            }
        except Exception:
            cid = generate_correlation_id()
            logger.exception("entities_resource failed", extra={"correlation_id": cid})
            return {"error": f"Failed to retrieve entity counts. Error ID: {cid}"}

    async def domain_counts(self) -> dict[str, Any]:
        try:
            counts: dict[str, int] = {}
            for payload in await self._summary_payloads():
                metadata = ensure_mapping(payload.get("metadata"))
                domain = metadata.get("domain", "")
                if domain:
                    counts[domain] = counts.get(domain, 0) + 1

            sorted_domains = sorted(counts.items(), key=lambda item: item[1], reverse=True)
            return {
                "domains": [{"domain": domain, "count": count} for domain, count in sorted_domains],
                "total_unique_domains": len(sorted_domains),
            }
        except Exception:
            cid = generate_correlation_id()
            logger.exception("domains_resource failed", extra={"correlation_id": cid})
            return {"error": f"Failed to retrieve domain counts. Error ID: {cid}"}

    def _summary_stmt(self, request_model: Any, summary_model: Any) -> Any:
        return (
            select(summary_model)
            .join(request_model, summary_model.request_id == request_model.id)
            .options(selectinload(summary_model.request))
            .where(
                summary_model.is_deleted.is_(False),
                *self.context.request_scope_filters(request_model),
            )
        )

    async def _summary_count(
        self,
        session: Any,
        request_model: Any,
        summary_model: Any,
        extra_filters: Iterable[ColumnElement[bool]] = (),
    ) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(summary_model)
                .join(request_model, summary_model.request_id == request_model.id)
                .where(
                    summary_model.is_deleted.is_(False),
                    *self.context.request_scope_filters(request_model),
                    *extra_filters,
                )
            )
            or 0
        )

    async def _request_count(self, session: Any, request_model: Any, request_type: str) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(request_model)
                .where(
                    *self.context.request_scope_filters(request_model),
                    request_model.type == request_type,
                )
            )
            or 0
        )

    async def _summary_payloads(self) -> list[dict[str, Any]]:
        from app.db.models import Request, Summary

        runtime = self.context.ensure_runtime()
        async with runtime.database.session() as session:
            rows = await session.execute(
                select(Summary.json_payload)
                .join(Request, Summary.request_id == Request.id)
                .where(
                    Summary.is_deleted.is_(False),
                    *self.context.request_scope_filters(Request),
                )
                .order_by(Summary.created_at.desc())
                # Cap at _ENTITY_SCAN_LIMIT to prevent unbounded memory growth.
                # Aggregate callers (tag_counts, entity_counts, domain_counts)
                # operate on a representative recent window, not the full corpus.
                .limit(self._ENTITY_SCAN_LIMIT)
            )
            return [ensure_mapping(row[0]) for row in rows]

    async def _aggregate_payload_values(self, key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for payload in await self._summary_payloads():
            for item in payload.get(key, []):
                counts[item] = counts.get(item, 0) + 1
        return counts
