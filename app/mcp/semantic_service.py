from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import numpy as np
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.mcp.helpers import (
    McpErrorResult,
    clamp_limit,
    clamp_similarity,
    ensure_mapping,
    format_summary_compact,
    safe_int,
)

logger = logging.getLogger("ratatoskr.mcp")

if TYPE_CHECKING:
    from app.mcp.article_service import ArticleReadService
    from app.mcp.context import McpServerContext


class SemanticSearchService:
    def __init__(self, context: McpServerContext, article_service: ArticleReadService) -> None:
        self.context = context
        self.article_service = article_service

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        if not text:
            return set()
        return {token.lower() for token in re.findall(r"[\w-]{2,}", text, flags=re.UNICODE)}

    def _lexical_overlap_score(self, query: str, text: str) -> float:
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return 0.0
        text_tokens = self._tokenize(text)
        if not text_tokens:
            return 0.0
        return len(query_tokens.intersection(text_tokens)) / len(query_tokens)

    @staticmethod
    def _cosine_similarity(query_vector: list[float], candidate_vector: list[float]) -> float:
        if not query_vector or not candidate_vector:
            return 0.0
        q = np.array(query_vector, dtype=np.float64)
        c = np.array(candidate_vector, dtype=np.float64)
        q_norm = np.linalg.norm(q)
        c_norm = np.linalg.norm(c)
        if q_norm <= 0.0 or c_norm <= 0.0:
            return 0.0
        return float(np.clip(np.dot(q, c) / (q_norm * c_norm), 0.0, 1.0))

    @staticmethod
    def _extract_query_tags(text: str) -> list[str]:
        if not text:
            return []
        tags = [
            f"#{match.lower()}" for match in re.findall(r"#([\w-]{1,50})", text, flags=re.UNICODE)
        ]
        seen: set[str] = set()
        ordered: list[str] = []
        for tag in tags:
            if tag in seen:
                continue
            seen.add(tag)
            ordered.append(tag)
        return ordered

    @staticmethod
    def extract_semantic_seed_text(payload: dict[str, Any]) -> str:
        metadata = ensure_mapping(payload.get("metadata"))
        pieces: list[str] = []
        for value in (
            metadata.get("title"),
            payload.get("summary_250"),
            payload.get("tldr"),
            payload.get("summary_1000"),
        ):
            if value:
                pieces.append(str(value))
        for idea in payload.get("key_ideas", [])[:5]:
            if idea:
                pieces.append(str(idea))
        for tag in payload.get("topic_tags", [])[:8]:
            if tag:
                pieces.append(str(tag))
        return " ".join(pieces).strip()

    async def _fetch_summaries_by_ids(self, summary_ids: list[int]) -> dict[int, tuple[Any, Any]]:
        from app.db.models import Request, Summary

        if not summary_ids:
            return {}

        runtime = self.context.ensure_runtime()
        async with runtime.database.session() as session:
            rows = (
                await session.scalars(
                    select(Summary)
                    .join(Request, Summary.request_id == Request.id)
                    .options(selectinload(Summary.request))
                    .where(
                        Summary.id.in_(summary_ids),
                        Summary.is_deleted.is_(False),
                        *self.context.request_scope_filters(Request),
                    )
                )
            ).all()

        return {int(summary.id): (summary, summary.request) for summary in rows}

    @staticmethod
    def _semantic_match_from_row(row: dict[str, Any]) -> dict[str, Any]:
        preview = row.get("local_summary") or row.get("snippet") or row.get("text")
        preview_text = str(preview) if preview else ""
        if len(preview_text) > 320:
            preview_text = preview_text[:317] + "..."

        return {
            "similarity_score": round(float(row.get("similarity_score", 0.0)), 4),
            "window_id": row.get("window_id"),
            "window_index": row.get("window_index"),
            "chunk_id": row.get("chunk_id"),
            "section": row.get("section"),
            "topics": row.get("topics") or [],
            "keywords": row.get("local_keywords") or [],
            "semantic_boosters": row.get("semantic_boosters") or [],
            "preview": preview_text,
        }

    async def _build_semantic_results(
        self,
        *,
        query: str,
        rows: list[dict[str, Any]],
        backend: str,
        limit: int,
        include_chunks: bool,
        rerank: bool,
    ) -> list[dict[str, Any]]:
        grouped: dict[int, dict[str, Any]] = {}

        for row in rows:
            raw_summary_id = row.get("summary_id")
            try:
                summary_id = int(raw_summary_id)
            except (TypeError, ValueError) as exc:
                logger.debug(
                    "mcp_semantic_row_invalid_summary_id",
                    extra={"summary_id": str(raw_summary_id), "error": str(exc)},
                )
                continue

            score = float(row.get("similarity_score", 0.0))
            group = grouped.get(summary_id)
            if group is None:
                group = {
                    "summary_id": summary_id,
                    "similarity_score": score,
                    "best_row": row,
                    "matches": [],
                }
                grouped[summary_id] = group
            elif score > float(group.get("similarity_score", 0.0)):
                group["similarity_score"] = score
                group["best_row"] = row

            if include_chunks:
                match = self._semantic_match_from_row(row)
                signature = (
                    match.get("window_id"),
                    match.get("chunk_id"),
                    match.get("section"),
                    match.get("preview"),
                )
                seen_signatures = group.setdefault("seen_signatures", set())
                if signature not in seen_signatures:
                    seen_signatures.add(signature)
                    group["matches"].append(match)

        if not grouped:
            return []

        summary_map = await self._fetch_summaries_by_ids(list(grouped.keys()))
        results: list[dict[str, Any]] = []

        for summary_id, group in grouped.items():
            summary_bundle = summary_map.get(summary_id)
            if summary_bundle is None:
                continue
            summary_row, request_row = summary_bundle
            compact = format_summary_compact(summary_row, request_row)
            compact["similarity_score"] = round(float(group["similarity_score"]), 4)
            compact["search_backend"] = backend

            best_row = group.get("best_row") or {}
            compact["semantic_context"] = {
                "section": best_row.get("section"),
                "topics": best_row.get("topics") or [],
                "keywords": best_row.get("local_keywords") or [],
            }

            if include_chunks:
                matches = sorted(
                    group.get("matches", []),
                    key=lambda item: item.get("similarity_score", 0.0),
                    reverse=True,
                )
                compact["semantic_matches"] = matches[:5]
                compact["semantic_match_count"] = len(matches)
                compact["best_match"] = matches[0] if matches else None

            results.append(compact)

        if rerank and results:
            for result in results:
                text = " ".join(
                    [
                        str(result.get("title") or ""),
                        str(result.get("summary_250") or ""),
                        str(result.get("tldr") or ""),
                        str(ensure_mapping(result.get("best_match")).get("preview") or ""),
                    ]
                )
                lexical = self._lexical_overlap_score(query, text)
                result["rerank_score"] = round(
                    0.82 * float(result.get("similarity_score", 0.0)) + 0.18 * lexical,
                    4,
                )

            results.sort(key=lambda item: float(item.get("rerank_score", 0.0)), reverse=True)
        else:
            results.sort(key=lambda item: float(item.get("similarity_score", 0.0)), reverse=True)

        return results[:limit]

    async def _search_local_vectors(
        self,
        query: str,
        *,
        language: str | None,
        limit: int,
        min_similarity: float,
    ) -> list[dict[str, Any]]:
        from app.db.models import (
            Request,
            Summary,
            SummaryEmbedding,
        )

        embedding_service = await self.context.init_local_vector_service()
        if embedding_service is None:
            return []

        try:
            query_vector_any = await embedding_service.generate_embedding(
                query.strip(),
                language=language,
                task_type="query",
            )
        except Exception:
            logger.exception("local_vector_query_embedding_failed")
            return []

        query_vector = (
            query_vector_any.tolist()
            if hasattr(query_vector_any, "tolist")
            else list(query_vector_any)
        )
        if not query_vector:
            return []

        scan_limit = max(limit * 80, 600)
        runtime = self.context.ensure_runtime()
        async with runtime.database.session() as session:
            stmt = (
                select(SummaryEmbedding)
                .join(Summary, SummaryEmbedding.summary_id == Summary.id)
                .join(Request, Summary.request_id == Request.id)
                .options(selectinload(SummaryEmbedding.summary).selectinload(Summary.request))
                .where(
                    Summary.is_deleted.is_(False),
                    *self.context.request_scope_filters(Request),
                )
                .order_by(SummaryEmbedding.created_at.desc())
                .limit(scan_limit)
            )
            if language:
                stmt = stmt.where(or_(Summary.lang == language, Summary.lang.is_(None)))
            query_rows = (await session.scalars(stmt)).all()

        rows_data = []
        for row in query_rows:
            payload = ensure_mapping(getattr(row.summary, "json_payload", None))
            metadata = ensure_mapping(payload.get("metadata"))
            snippet = payload.get("summary_250") or payload.get("tldr")
            rows_data.append(
                {
                    "embedding_blob": row.embedding_blob,
                    "request_id": getattr(row.summary.request, "id", None),
                    "summary_id": getattr(row.summary, "id", None),
                    "url": getattr(row.summary.request, "input_url", None)
                    or getattr(row.summary.request, "normalized_url", None),
                    "title": metadata.get("title"),
                    "snippet": snippet,
                    "text": payload.get("summary_1000") or snippet,
                    "source": metadata.get("domain"),
                    "published_at": metadata.get("published_at"),
                    "local_summary": snippet,
                    "topics": payload.get("topic_tags", []),
                    "local_keywords": payload.get("seo_keywords", []),
                }
            )

        def _compute() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for row_dict in rows_data:
                candidate_row = dict(row_dict)
                try:
                    candidate = embedding_service.deserialize_embedding(
                        candidate_row.pop("embedding_blob")
                    )
                except Exception as exc:
                    logger.debug(
                        "mcp_local_vector_deserialize_failed",
                        extra={"summary_id": candidate_row.get("summary_id"), "error": str(exc)},
                    )
                    continue
                similarity = self._cosine_similarity(query_vector, candidate)
                if similarity < min_similarity:
                    continue
                candidate_row["similarity_score"] = similarity
                results.append(candidate_row)

            results.sort(key=lambda item: float(item.get("similarity_score", 0.0)), reverse=True)
            return results[: max(limit * 4, limit)]

        return await asyncio.to_thread(_compute)

    async def _run_semantic_candidates(
        self,
        query: str,
        *,
        language: str | None,
        limit: int,
        min_similarity: float,
        include_chunks: bool,
        rerank: bool,
        allow_keyword_fallback: bool,
    ) -> dict[str, Any]:
        limit = clamp_limit(limit)
        min_similarity = clamp_similarity(min_similarity)
        fetch_limit = max(limit * 6, limit + 8)

        vector_svc = await self.context.init_vector_service()
        if vector_svc is not None:
            try:
                vector_results = await vector_svc.search(
                    query.strip(),
                    language=language,
                    tags=self._extract_query_tags(query),
                    user_id=self.context.user_id,
                    limit=fetch_limit,
                    offset=0,
                )

                vector_rows: list[dict[str, Any]] = []
                for hit in vector_results.results:
                    if float(hit.similarity_score) < min_similarity:
                        continue
                    vector_rows.append(
                        {
                            "request_id": hit.request_id,
                            "summary_id": hit.summary_id,
                            "similarity_score": hit.similarity_score,
                            "url": hit.url,
                            "title": hit.title,
                            "snippet": hit.snippet,
                            "text": hit.text,
                            "source": hit.source,
                            "published_at": hit.published_at,
                            "window_id": hit.window_id,
                            "window_index": hit.window_index,
                            "chunk_id": hit.chunk_id,
                            "section": hit.section,
                            "topics": hit.topics,
                            "local_keywords": hit.local_keywords,
                            "semantic_boosters": hit.semantic_boosters,
                            "local_summary": hit.local_summary,
                        }
                    )

                enriched = await self._build_semantic_results(
                    query=query,
                    rows=vector_rows,
                    backend="vector",
                    limit=limit,
                    include_chunks=include_chunks,
                    rerank=rerank,
                )
                if enriched:
                    return {
                        "results": enriched,
                        "has_more": bool(vector_results.has_more),
                        "search_type": "semantic",
                        "search_backend": "vector",
                    }
            except Exception:
                logger.exception("semantic_vector_search_failed")

        local_rows = await self._search_local_vectors(
            query,
            language=language,
            limit=fetch_limit,
            min_similarity=min_similarity,
        )
        enriched_local = await self._build_semantic_results(
            query=query,
            rows=local_rows,
            backend="local_vector",
            limit=limit,
            include_chunks=include_chunks,
            rerank=rerank,
        )
        if enriched_local:
            return {
                "results": enriched_local,
                "has_more": len(enriched_local) >= limit and len(local_rows) > len(enriched_local),
                "search_type": "semantic",
                "search_backend": "local_vector",
            }

        if allow_keyword_fallback:
            keyword_payload = await self.article_service.search_articles(query=query, limit=limit)
            if "error" in keyword_payload:
                keyword_payload = {}
            keyword_results = keyword_payload.get("results")
            if not isinstance(keyword_results, list):
                keyword_results = []
            return {
                "results": keyword_results[:limit],
                "has_more": bool(int(keyword_payload.get("total") or 0) > limit),
                "search_type": "keyword_fallback",
                "search_backend": "fts",
            }

        return {
            "results": [],
            "has_more": False,
            "search_type": "semantic",
            "search_backend": "none",
        }

    async def semantic_search(
        self,
        description: str,
        limit: int = 10,
        language: str | None = None,
        min_similarity: float = 0.25,
        rerank: bool = False,
        include_chunks: bool = True,
    ) -> dict[str, Any]:
        try:
            payload = await self._run_semantic_candidates(
                description,
                language=language,
                limit=limit,
                min_similarity=min_similarity,
                include_chunks=include_chunks,
                rerank=rerank,
                allow_keyword_fallback=True,
            )
            return {
                "results": payload.get("results", []),
                "total": len(payload.get("results", [])),
                "query": description,
                "search_type": payload.get("search_type", "semantic"),
                "search_backend": payload.get("search_backend", "none"),
                "has_more": bool(payload.get("has_more", False)),
                "min_similarity": round(clamp_similarity(min_similarity), 4),
                "rerank_applied": bool(rerank),
            }
        except Exception as exc:
            logger.exception("semantic_search failed")
            return {"error": str(exc), "query": description}

    async def hybrid_search(
        self,
        query: str,
        limit: int = 10,
        language: str | None = None,
        min_similarity: float = 0.25,
        rerank: bool = False,
    ) -> dict[str, Any]:
        limit = clamp_limit(limit)

        try:
            semantic = await self._run_semantic_candidates(
                query,
                language=language,
                limit=max(limit * 2, 12),
                min_similarity=min_similarity,
                include_chunks=True,
                rerank=rerank,
                allow_keyword_fallback=False,
            )
            semantic_results = semantic.get("results", [])
            if not isinstance(semantic_results, list):
                semantic_results = []

            keyword_payload = await self.article_service.search_articles(
                query=query, limit=max(limit * 2, 12)
            )
            keyword_results = keyword_payload.get("results", [])
            if not isinstance(keyword_results, list):
                keyword_results = []

            fused: dict[int, dict[str, Any]] = {}
            fusion_k = 50.0

            for index, item in enumerate(semantic_results):
                summary_id = safe_int(ensure_mapping(item).get("summary_id"))
                if summary_id is None:
                    continue
                bucket = fused.setdefault(summary_id, dict(item))
                bucket.setdefault("match_sources", [])
                if "semantic" not in bucket["match_sources"]:
                    bucket["match_sources"].append("semantic")
                bucket["hybrid_score"] = float(bucket.get("hybrid_score", 0.0)) + (
                    1.0 / (fusion_k + index)
                )
                bucket["semantic_score"] = float(item.get("similarity_score", 0.0))

            for index, item in enumerate(keyword_results):
                summary_id = safe_int(ensure_mapping(item).get("summary_id"))
                if summary_id is None:
                    continue
                if summary_id not in fused:
                    fused[summary_id] = dict(item)
                    fused[summary_id]["semantic_score"] = None
                bucket = fused[summary_id]
                bucket.setdefault("match_sources", [])
                if "keyword" not in bucket["match_sources"]:
                    bucket["match_sources"].append("keyword")
                bucket["hybrid_score"] = float(bucket.get("hybrid_score", 0.0)) + (
                    1.0 / (fusion_k + index)
                )

            results = sorted(
                fused.values(),
                key=lambda item: float(item.get("hybrid_score", 0.0)),
                reverse=True,
            )
            for row in results:
                row["hybrid_score"] = round(float(row.get("hybrid_score", 0.0)), 4)

            return {
                "results": results[:limit],
                "total": min(len(results), limit),
                "query": query,
                "search_type": "hybrid",
                "semantic_backend": semantic.get("search_backend", "none"),
                "min_similarity": round(clamp_similarity(min_similarity), 4),
                "rerank_applied": bool(rerank),
                "has_more": len(results) > limit,
            }
        except Exception as exc:
            logger.exception("hybrid_search failed")
            return {"error": str(exc), "query": query}

    async def find_similar_articles(
        self,
        summary_id: int,
        limit: int = 10,
        min_similarity: float = 0.3,
        rerank: bool = False,
        include_chunks: bool = True,
    ) -> dict[str, Any]:
        from app.db.models import Request, Summary

        limit = clamp_limit(limit)

        try:
            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                source_summary = await session.scalar(
                    select(Summary)
                    .join(Request, Summary.request_id == Request.id)
                    .options(selectinload(Summary.request))
                    .where(
                        Summary.id == summary_id,
                        Summary.is_deleted.is_(False),
                        *self.context.request_scope_filters(Request),
                    )
                )
            if source_summary is None:
                return {"error": f"Summary {summary_id} not found"}
        except Exception as exc:
            logger.exception("find_similar_articles source lookup failed")
            return {"error": str(exc), "summary_id": summary_id}

        payload = ensure_mapping(getattr(source_summary, "json_payload", None))
        seed_query = self.extract_semantic_seed_text(payload)
        if not seed_query:
            return {
                "summary_id": summary_id,
                "results": [],
                "total": 0,
                "message": "Source summary has no text suitable for semantic search",
            }

        try:
            semantic = await self._run_semantic_candidates(
                seed_query,
                language=getattr(source_summary, "lang", None),
                limit=max(limit + 4, 12),
                min_similarity=min_similarity,
                include_chunks=include_chunks,
                rerank=rerank,
                allow_keyword_fallback=False,
            )
            raw_results = semantic.get("results", [])
            if not isinstance(raw_results, list):
                raw_results = []
            results = [
                row
                for row in raw_results
                if safe_int(ensure_mapping(row).get("summary_id")) != int(summary_id)
            ][:limit]
            return {
                "summary_id": summary_id,
                "query_seed": seed_query[:500],
                "results": results,
                "total": len(results),
                "search_type": "similarity",
                "search_backend": semantic.get("search_backend", "none"),
                "min_similarity": round(clamp_similarity(min_similarity), 4),
                "rerank_applied": bool(rerank),
                "has_more": len(raw_results) > len(results),
            }
        except Exception as exc:
            logger.exception("find_similar_articles failed")
            return {"error": str(exc), "summary_id": summary_id}

    async def vector_health(self) -> dict[str, Any] | McpErrorResult:
        try:
            vector_svc = await self.context.init_vector_service()
            local = await self.context.init_local_vector_service()
            vector_store = getattr(vector_svc, "_vector_store", None) if vector_svc else None

            now = time.monotonic()
            vector_failed_for = (
                round(now - self.context.vector_last_failed_at, 2)
                if self.context.vector_last_failed_at is not None
                else None
            )
            local_failed_for = (
                round(now - self.context.local_vector_last_failed_at, 2)
                if self.context.local_vector_last_failed_at is not None
                else None
            )

            return {
                "vector_available": bool(vector_svc is not None),
                "local_vector_available": bool(local is not None),
                "collection_name": getattr(vector_store, "collection_name", None),
                "environment": getattr(vector_store, "environment", None),
                "user_scope": getattr(vector_store, "user_scope", None),
                "vector_last_failed_seconds_ago": vector_failed_for,
                "local_last_failed_seconds_ago": local_failed_for,
            }
        except Exception as exc:
            logger.exception("vector_health failed")
            return {"error": str(exc)}

    async def vector_index_stats(self, scan_limit: int = 5000) -> dict[str, Any]:
        from app.db.models import Request, Summary

        scan_limit = max(100, min(50000, int(scan_limit)))

        try:
            vector_svc = await self.context.init_vector_service()
            if vector_svc is None:
                return {"error": "Vector store unavailable", "vector_available": False}

            vector_store = getattr(vector_svc, "_vector_store", None)
            if vector_store is None:
                return {"error": "Vector store unavailable", "vector_available": False}

            vector_ids = vector_store.get_indexed_summary_ids(
                user_id=self.context.user_id, limit=scan_limit
            )
            overlap_count = 0
            database_count = 0
            offset = 0
            batch_size = 500

            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                while True:
                    rows = await session.execute(
                        select(Summary.id)
                        .join(Request, Summary.request_id == Request.id)
                        .where(
                            Summary.is_deleted.is_(False),
                            *self.context.request_scope_filters(Request),
                        )
                        .order_by(Summary.created_at.desc())
                        .limit(batch_size)
                        .offset(offset)
                    )
                    batch_ids = {int(row[0]) for row in rows}
                    if not batch_ids:
                        break
                    database_count += len(batch_ids)
                    overlap_count += len(batch_ids.intersection(vector_ids))
                    offset += batch_size
                    if database_count >= scan_limit:
                        break

            coverage_pct = (
                round((overlap_count / database_count * 100), 2) if database_count else 0.0
            )
            return {
                "vector_available": True,
                "user_scope_id": self.context.user_id,
                "scan_limit": scan_limit,
                "database_summary_count": database_count,
                "vector_indexed_count": len(vector_ids),
                "overlap_count": overlap_count,
                "coverage_percent": coverage_pct,
            }
        except Exception as exc:
            logger.exception("vector_index_stats failed")
            return {"error": str(exc)}

    async def vector_sync_gap(self, max_scan: int = 5000, sample_size: int = 20) -> dict[str, Any]:
        from app.db.models import Request, Summary

        max_scan = max(100, min(50000, int(max_scan)))
        sample_size = max(1, min(100, int(sample_size)))

        try:
            vector_svc = await self.context.init_vector_service()
            if vector_svc is None:
                return {"error": "Vector store unavailable", "vector_available": False}

            vector_store = getattr(vector_svc, "_vector_store", None)
            if vector_store is None:
                return {"error": "Vector store unavailable", "vector_available": False}

            vector_ids = vector_store.get_indexed_summary_ids(
                user_id=self.context.user_id, limit=max_scan
            )
            missing_in_vector = set()
            missing_in_database = set(vector_ids)
            database_count = 0
            offset = 0
            batch_size = 500

            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                while True:
                    rows = await session.execute(
                        select(Summary.id)
                        .join(Request, Summary.request_id == Request.id)
                        .where(
                            Summary.is_deleted.is_(False),
                            *self.context.request_scope_filters(Request),
                        )
                        .order_by(Summary.created_at.desc())
                        .limit(batch_size)
                        .offset(offset)
                    )
                    batch_ids = {int(row[0]) for row in rows}
                    if not batch_ids:
                        break
                    database_count += len(batch_ids)
                    missing_in_vector.update(batch_ids - vector_ids)
                    missing_in_database.difference_update(batch_ids)
                    offset += batch_size
                    if database_count >= max_scan:
                        break

            sorted_missing_vector = sorted(missing_in_vector)
            sorted_missing_database = sorted(missing_in_database)
            return {
                "vector_available": True,
                "user_scope_id": self.context.user_id,
                "max_scan": max_scan,
                "database_summary_count": database_count,
                "vector_indexed_count": len(vector_ids),
                "missing_in_vector_count": len(sorted_missing_vector),
                "missing_in_database_count": len(sorted_missing_database),
                "missing_in_vector_sample": sorted_missing_vector[:sample_size],
                "missing_in_database_sample": sorted_missing_database[:sample_size],
            }
        except Exception as exc:
            logger.exception("vector_sync_gap failed")
            return {"error": str(exc)}
