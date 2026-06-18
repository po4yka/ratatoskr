"""Qdrant indexed-entity scroll and deletion helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from qdrant_client.models import FieldCondition, Filter, MatchValue, PointIdsList

from app.core.logging_utils import get_logger
from app.infrastructure.vector.point_ids import git_mirror_point_id, str_to_uuid as _str_to_uuid
from app.infrastructure.vector.protocol import VectorStoreError
from app.observability.metrics import record_vector_write

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)


class QdrantIndexedEntityMixin:
    """Mixin for Qdrant entity index inventory and point deletion helpers."""

    def get_indexed_summary_ids(
        self: Any, *, user_id: int | None = None, limit: int | None = 5000
    ) -> set[int]:
        if not self._available:
            self.ensure_available()
        if not self._available:
            return set()

        # The collection is shared with repository and x_wiki points. Exclude
        # them via must_not so a summary scroll can never return another
        # entity's points. must_not (rather than a positive entity_type ==
        # "summary" match) keeps legacy summary points that predate the
        # entity_type payload field.
        scroll_filter = Filter(
            must=[
                FieldCondition(key="environment", match=MatchValue(value=self._environment)),
                FieldCondition(key="user_scope", match=MatchValue(value=self._user_scope)),
                *(
                    [FieldCondition(key="user_id", match=MatchValue(value=user_id))]
                    if user_id is not None
                    else []
                ),
            ],
            must_not=[
                FieldCondition(key="entity_type", match=MatchValue(value="repository")),
                FieldCondition(key="entity_type", match=MatchValue(value="x_wiki")),
            ],
        )
        try:
            records = self._scroll_all(
                scroll_filter=scroll_filter,
                with_payload=["summary_id"],
                page_size=limit or 5000,
            )
            summary_ids: set[int] = set()
            for record in records:
                raw = (record.payload or {}).get("summary_id")
                try:
                    if raw is not None:
                        summary_ids.add(int(raw))
                except (TypeError, ValueError):
                    continue
            return summary_ids
        except Exception as exc:
            logger.error("vector_get_indexed_summary_ids_failed", extra={"error": str(exc)})
            if self._required:
                raise VectorStoreError(str(exc)) from exc
            return set()

    def get_indexed_repository_ids(
        self: Any, *, user_id: int | None = None, limit: int | None = 5000
    ) -> set[int]:
        return cast(
            "set[int]",
            self._get_indexed_int_payloads(
                entity_type="repository",
                payload_key="repository_id",
                log_event="vector_get_indexed_repository_ids_failed",
                user_id=user_id,
                limit=limit,
            ),
        )

    def get_indexed_git_mirror_ids(
        self: Any, *, user_id: int | None = None, limit: int | None = 5000
    ) -> set[int]:
        return cast(
            "set[int]",
            self._get_indexed_int_payloads(
                entity_type="git_mirror",
                payload_key="mirror_id",
                log_event="vector_get_indexed_git_mirror_ids_failed",
                user_id=user_id,
                limit=limit,
            ),
        )

    def _get_indexed_int_payloads(
        self: Any,
        *,
        entity_type: str,
        payload_key: str,
        log_event: str,
        user_id: int | None,
        limit: int | None,
    ) -> set[int]:
        if not self._available:
            self.ensure_available()
        if not self._available:
            return set()

        scroll_filter = self._entity_filter(entity_type=entity_type, user_id=user_id)
        try:
            records = self._scroll_all(
                scroll_filter=scroll_filter,
                with_payload=[payload_key],
                page_size=limit or 5000,
            )
            ids: set[int] = set()
            for record in records:
                raw = (record.payload or {}).get(payload_key)
                try:
                    if raw is not None:
                        ids.add(int(raw))
                except (TypeError, ValueError):
                    continue
            return ids
        except Exception as exc:
            logger.error(log_event, extra={"error": str(exc)})
            if self._required:
                raise VectorStoreError(str(exc)) from exc
            return set()

    def get_indexed_x_wiki_paths(
        self: Any, *, user_id: int | None = None, limit: int | None = 5000
    ) -> set[str]:
        if not self._available:
            self.ensure_available()
        if not self._available:
            return set()

        scroll_filter = self._entity_filter(entity_type="x_wiki", user_id=user_id)
        try:
            records = self._scroll_all(
                scroll_filter=scroll_filter,
                with_payload=["wiki_path"],
                page_size=limit or 5000,
            )
            wiki_paths: set[str] = set()
            for record in records:
                raw = (record.payload or {}).get("wiki_path")
                if isinstance(raw, str) and raw:
                    wiki_paths.add(raw)
            return wiki_paths
        except Exception as exc:
            logger.error("vector_get_indexed_x_wiki_paths_failed", extra={"error": str(exc)})
            if self._required:
                raise VectorStoreError(str(exc)) from exc
            return set()

    def get_indexed_x_wiki_path_hashes(
        self: Any, *, user_id: int | None = None, limit: int | None = 5000
    ) -> dict[str, str]:
        """Return {wiki_path: content_hash} for x_wiki points.

        Sibling to ``get_indexed_x_wiki_paths`` — kept separate so
        callers that only need the path set retain a stable contract while
        drift-detection callers (``XWikiSyncService``) get the
        payload's ``content_hash`` field in the same scroll.
        """
        if not self._available:
            self.ensure_available()
        if not self._available:
            return {}

        scroll_filter = self._entity_filter(entity_type="x_wiki", user_id=user_id)
        try:
            records = self._scroll_all(
                scroll_filter=scroll_filter,
                with_payload=["wiki_path", "content_hash"],
                page_size=limit or 5000,
            )
            path_hashes: dict[str, str] = {}
            for record in records:
                payload = record.payload or {}
                raw_path = payload.get("wiki_path")
                raw_hash = payload.get("content_hash")
                if (
                    isinstance(raw_path, str)
                    and raw_path
                    and isinstance(raw_hash, str)
                    and raw_hash
                ):
                    path_hashes[raw_path] = raw_hash
            return path_hashes
        except Exception as exc:
            logger.error(
                "vector_get_indexed_x_wiki_path_hashes_failed",
                extra={"error": str(exc)},
            )
            if self._required:
                raise VectorStoreError(str(exc)) from exc
            return {}

    def delete_x_wiki_paths(self: Any, wiki_paths: Sequence[str]) -> None:
        """Delete x_wiki points keyed by their wiki path strings.

        Uses the same ``str_to_uuid`` derivation as the upsert path so the
        delete is symmetric with ``upsert_notes(..., ids=[<wiki_path>])``.
        """
        if not wiki_paths:
            return
        if not self._available:
            self.ensure_available()
        if not self._available:
            logger.warning(
                "vector_delete_skipped",
                extra={"reason": "not_available", "count": len(list(wiki_paths))},
            )
            return
        point_ids = [_str_to_uuid(p) for p in wiki_paths]
        try:
            self._client.delete(
                collection_name=self._collection_name,
                points_selector=PointIdsList(points=list(point_ids)),
                wait=True,
            )
        except Exception as exc:
            logger.error(
                "vector_delete_x_wiki_paths_failed",
                extra={"count": len(point_ids), "error": str(exc)},
            )
            record_vector_write(operation="delete", status="failed")
            if self._required:
                raise VectorStoreError(str(exc)) from exc
            self._available = False

    def delete_git_mirror_points(self: Any, mirror_ids: Sequence[int]) -> None:
        """Delete git_mirror points keyed by their mirror ids.

        Uses the same ``git_mirror_point_id`` derivation as the indexer's upsert
        path so deletion is symmetric with how points are written.
        """
        if not mirror_ids:
            return
        if not self._available:
            self.ensure_available()
        if not self._available:
            logger.warning(
                "vector_delete_skipped",
                extra={"reason": "not_available", "count": len(list(mirror_ids))},
            )
            return
        point_ids = [
            git_mirror_point_id(self._environment, self._user_scope, mid) for mid in mirror_ids
        ]
        try:
            self._client.delete(
                collection_name=self._collection_name,
                points_selector=PointIdsList(points=list(point_ids)),
                wait=True,
            )
        except Exception as exc:
            logger.error(
                "vector_delete_git_mirror_points_failed",
                extra={"count": len(point_ids), "error": str(exc)},
            )
            record_vector_write(operation="delete", status="failed")
            if self._required:
                raise VectorStoreError(str(exc)) from exc
            self._available = False

    def _entity_filter(self: Any, *, entity_type: str, user_id: int | None) -> Filter:
        return Filter(
            must=[
                FieldCondition(key="environment", match=MatchValue(value=self._environment)),
                FieldCondition(key="user_scope", match=MatchValue(value=self._user_scope)),
                FieldCondition(key="entity_type", match=MatchValue(value=entity_type)),
                *(
                    [FieldCondition(key="user_id", match=MatchValue(value=user_id))]
                    if user_id is not None
                    else []
                ),
            ]
        )
