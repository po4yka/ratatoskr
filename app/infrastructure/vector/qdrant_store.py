"""Qdrant-backed vector store implementing the VectorStore protocol."""

from __future__ import annotations

import asyncio
import time
import warnings
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchAny,
    MatchValue,
    PayloadSchemaType,
    PointIdsList,
    PointStruct,
    VectorParams,
)

from app.core.logging_utils import get_logger
from app.infrastructure.vector.point_ids import str_to_uuid as _str_to_uuid
from app.infrastructure.vector.protocol import VectorStoreError
from app.infrastructure.vector.qdrant_indexed_entities import QdrantIndexedEntityMixin
from app.infrastructure.vector.qdrant_schemas import QdrantQueryFilters
from app.infrastructure.vector.result_types import VectorQueryHit, VectorQueryResult
from app.observability.attributes import VECTOR_OPERATION, VECTOR_STATUS
from app.observability.metrics import record_db_query, record_vector_write

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)


def _get_tracer() -> Any:
    from app.observability.otel import get_tracer

    return get_tracer(__name__)


# Upsert points are chunked into bounded batches so a large backfill does not
# build one oversized request body (Qdrant streams each chunk independently).
_UPSERT_CHUNK_SIZE = 256
_DELETE_FILTER_CHUNK_SIZE = 100


def _write_acknowledged(result: Any, *, wait: bool) -> bool:
    """Return whether Qdrant acknowledged/completed a write operation."""
    status = getattr(result, "status", None)
    status_value = getattr(status, "value", status)
    if status_value == "completed":
        return True
    return not wait and status_value == "acknowledged"


def _require_write_acknowledgement(result: Any, *, wait: bool, operation: str) -> None:
    if not _write_acknowledged(result, wait=wait):
        status = getattr(getattr(result, "status", None), "value", None)
        raise VectorStoreError(f"Qdrant {operation} was not acknowledged (status={status!r})")


class QdrantVectorStore(QdrantIndexedEntityMixin):
    """Synchronous vector store wrapper around Qdrant.

    Uses the synchronous ``QdrantClient`` so callers can wrap it in
    ``asyncio.to_thread``.
    All connection retries use ``time.sleep`` (not ``asyncio.sleep``) so
    ``__init__`` is safe to call from inside a running event loop.

    Graceful degradation: when ``required=False`` (default), every public
    method logs a warning on failure rather than raising an exception.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None,
        environment: str,
        user_scope: str,
        collection_version: str = "v1",
        embedding_space: str | None = None,
        embedding_dim: int = 768,
        required: bool = False,
        connection_timeout: float = 10.0,
    ) -> None:
        if not url:
            msg = "Qdrant URL must be provided"
            raise ValueError(msg)

        self._url = url
        self._api_key = api_key
        self._environment = environment
        self._user_scope = user_scope
        self._collection_version = collection_version
        self._embedding_space = embedding_space
        self._embedding_dim = embedding_dim
        self._required = required
        self._connection_timeout = connection_timeout
        self._available = False
        self._client: QdrantClient | None = None
        self._collection_name = self._build_collection_name(
            environment, user_scope, collection_version, embedding_space
        )

        self._connect_with_retry()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    @property
    def environment(self) -> str:
        return self._environment

    @property
    def user_scope(self) -> str:
        return self._user_scope

    @property
    def collection_version(self) -> str:
        return self._collection_version

    @property
    def embedding_space(self) -> str | None:
        return self._embedding_space

    @property
    def collection_name(self) -> str:
        return self._collection_name

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_collection_name(
        environment: str,
        user_scope: str,
        version: str,
        embedding_space: str | None = None,
    ) -> str:
        """Build the collection name from environment/scope/version/space."""
        safe_env = environment.replace(" ", "_")
        safe_scope = user_scope.replace(" ", "_")
        safe_version = version.replace(" ", "_")
        base_name = f"notes_{safe_env}_{safe_scope}_{safe_version}"
        if not embedding_space:
            return base_name
        safe_es = "".join(
            c if c.isalnum() or c in {"-", "_"} else "_"
            for c in str(embedding_space).strip().lower()
        ).strip("_")
        return f"{base_name}_{safe_es}" if safe_es else base_name

    def _connect_with_retry(self, max_attempts: int = 3, base_delay: float = 2.0) -> None:
        # Blocking time.sleep retries stall the event loop when the store is
        # constructed during async startup (DI wiring reads `.available`
        # synchronously right after construction, so the connect must complete
        # in __init__ -- it cannot be deferred). On a running loop, do a single
        # bounded attempt and skip the sleeps; off-loop (CLI/worker/sync wiring)
        # keep the full backoff so a slow-to-start Qdrant is still picked up.
        try:
            asyncio.get_running_loop()
            on_event_loop = True
        except RuntimeError:
            on_event_loop = False
        attempts = 1 if on_event_loop else max_attempts
        for attempt in range(1, attempts + 1):
            if self._try_connect():
                return
            if attempt < attempts:
                delay = base_delay * attempt
                logger.info(
                    "vector_connect_retry",
                    extra={"attempt": attempt, "next_delay_sec": delay, "url": self._url},
                )
                time.sleep(delay)  # off event loop only -- safe

    def _try_connect(self) -> bool:
        try:
            client = QdrantClient(
                url=self._url,
                api_key=self._api_key,
                timeout=int(self._connection_timeout),
                check_compatibility=False,
            )
            client.get_collections()  # probe / auth check

            if client.collection_exists(self._collection_name):
                self._validate_collection_dimensions(client)
            else:
                client.create_collection(
                    collection_name=self._collection_name,
                    vectors_config=VectorParams(
                        size=self._embedding_dim,
                        distance=Distance.COSINE,
                    ),
                )
                for field, schema in [
                    ("request_id", PayloadSchemaType.INTEGER),
                    ("summary_id", PayloadSchemaType.INTEGER),
                    ("user_id", PayloadSchemaType.INTEGER),
                    ("environment", PayloadSchemaType.KEYWORD),
                    ("user_scope", PayloadSchemaType.KEYWORD),
                    ("language", PayloadSchemaType.KEYWORD),
                    ("tags", PayloadSchemaType.KEYWORD),
                ]:
                    with warnings.catch_warnings():
                        warnings.filterwarnings(
                            "ignore",
                            message="Payload indexes have no effect in the local Qdrant.*",
                            category=UserWarning,
                        )
                        client.create_payload_index(
                            collection_name=self._collection_name,
                            field_name=field,
                            field_schema=schema,
                        )

            self._client = client
            self._available = True
            logger.info(
                "vector_collection_initialized",
                extra={
                    "collection": self._collection_name,
                    "url": self._url,
                    "environment": self._environment,
                    "version": self._collection_version,
                },
            )
            return True
        except Exception as exc:
            logger.error(
                "vector_initialization_failed",
                extra={"url": self._url, "error": str(exc), "required": self._required},
            )
            self._available = False
            if self._required:
                raise VectorStoreError(str(exc)) from exc
            return False

    def _validate_collection_dimensions(self, client: QdrantClient) -> None:
        collection = client.get_collection(self._collection_name)
        actual_dim = self._extract_collection_vector_size(collection)
        if actual_dim is None:
            logger.warning(
                "vector_collection_dimension_unknown",
                extra={"collection": self._collection_name, "expected_dim": self._embedding_dim},
            )
            return
        if actual_dim == self._embedding_dim:
            return

        msg = (
            f"Qdrant collection '{self._collection_name}' has vector dimension {actual_dim}, "
            f"but configured embedding provider expects {self._embedding_dim}. "
            "Switch back to the matching embedding provider, set QDRANT_COLLECTION_VERSION "
            "or embedding-space config to a fresh collection, or re-index with "
            "`python -m app.cli.backfill_vector_store --force` after recreating the collection."
        )
        logger.error(
            "vector_collection_dimension_mismatch",
            extra={
                "collection": self._collection_name,
                "actual_dim": actual_dim,
                "expected_dim": self._embedding_dim,
                "embedding_space": self._embedding_space,
            },
        )
        raise VectorStoreError(msg)

    @staticmethod
    def _extract_collection_vector_size(collection: Any) -> int | None:
        config = getattr(collection, "config", None)
        params = getattr(config, "params", None)
        vectors = getattr(params, "vectors", None)
        if vectors is None and isinstance(params, Mapping):
            vectors = params.get("vectors")

        if isinstance(vectors, Mapping):
            size = vectors.get("size")
            if size is not None:
                return int(size)
            for vector_config in vectors.values():
                size = getattr(vector_config, "size", None)
                if size is None and isinstance(vector_config, Mapping):
                    size = vector_config.get("size")
                if size is not None:
                    return int(size)
            return None

        size = getattr(vectors, "size", None)
        if size is not None:
            return int(size)
        return None

    def ensure_available(self) -> bool:
        logger.info("vector_reconnect_attempt", extra={"url": self._url})
        return self._try_connect()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_id(metadata: dict[str, Any]) -> str:
        """Derive a stable string key from metadata."""
        request_id = metadata.get("request_id")
        summary_id = metadata.get("summary_id")
        chunk_id = metadata.get("chunk_id")
        window_id = metadata.get("window_id")

        if request_id is not None:
            base = str(request_id)
            if chunk_id:
                return f"{base}:{chunk_id}"
            if window_id:
                return f"{base}:{window_id}"
            if summary_id is not None:
                return f"{base}:{summary_id}"
            return base

        return uuid4().hex

    def _build_points(
        self,
        vectors: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, Any]],
        ids: Sequence[str],
    ) -> list[PointStruct]:
        points = []
        for vec, meta, raw_id in zip(vectors, metadatas, ids, strict=True):
            # Drop empty lists — Qdrant rejects them for KEYWORD-indexed array fields
            clean = {k: v for k, v in meta.items() if not (isinstance(v, list) and not v)}
            # Inject scope fields so query filters always match stored points
            clean["environment"] = self._environment
            clean["user_scope"] = self._user_scope
            points.append(
                PointStruct(
                    id=_str_to_uuid(raw_id),
                    vector=list(vec),
                    payload=clean,
                )
            )
        return points

    def _scroll_all(
        self,
        *,
        scroll_filter: Filter,
        with_payload: Any,
        page_size: int = 5000,
        max_records: int | None = None,
    ) -> list[Any]:
        """Scroll every matching point, following next_page_offset to exhaustion.

        A single ``client.scroll()`` returns at most ``limit`` points plus a
        ``next_page_offset``. Callers that need the complete set (reconciler
        drift detection) must page through to the end; otherwise large
        collections silently truncate and already-indexed points are reported
        as missing.

        ``max_records`` caps total records returned across all pages. When the
        cap is reached the scroll stops early and the caller receives a partial
        result (logged as a warning). This prevents unbounded memory growth on
        very large collections. Set to ``None`` to retain the previous
        unlimited behaviour (reconciler full-scan callers should leave it
        ``None``; ad-hoc callers that pass ``limit`` as ``page_size`` should
        consider passing ``max_records=limit`` to make the cap explicit).
        """
        client = self._client
        records: list[Any] = []
        offset: Any = None
        page = max(1, int(page_size))
        while True:
            if max_records is not None:
                remaining = max_records - len(records)
                if remaining <= 0:
                    logger.warning(
                        "vector_scroll_all_cap_reached",
                        extra={
                            "max_records": max_records,
                            "collection": self._collection_name,
                        },
                    )
                    break
                page = min(page, remaining)
            batch, offset = client.scroll(
                collection_name=self._collection_name,
                scroll_filter=scroll_filter,
                limit=page,
                with_payload=with_payload,
                with_vectors=False,
                offset=offset,
            )
            records.extend(batch)
            if offset is None:
                break
        return records

    def _fetch_request_point_ids(self, request_id: int | str) -> set[str]:
        """Return all Qdrant point UUID strings stored for a request.

        Raises the underlying Qdrant exception on failure so that callers
        (replace_request_notes, replace_summary_point) can run their own error
        path — logging, marking the store unavailable, and raising
        VectorStoreError when required=True.  Silently returning an empty set
        would cause the caller to skip the stale-point delete, leaving orphan
        points in the collection.
        """
        req_filter = Filter(
            must=[FieldCondition(key="request_id", match=MatchValue(value=int(request_id)))]
        )
        records = self._scroll_all(scroll_filter=req_filter, with_payload=False, page_size=10_000)
        return {str(r.id) for r in records}

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def upsert_notes(
        self,
        vectors: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, Any]],
        ids: Sequence[str] | None = None,
        *,
        wait: bool = True,
    ) -> bool:
        """Upsert vectors with metadata, chunked into bounded batches.

        ``wait=True`` (default) requires Qdrant to return ``completed`` for
        every chunk. With ``wait=False``, ``acknowledged`` is sufficient, so
        callers must not advance a durable SQL index cursor from that weaker
        result.
        """
        if not self._available:
            self.ensure_available()
        if not self._available:
            logger.warning(
                "vector_upsert_skipped", extra={"reason": "not_available", "count": len(vectors)}
            )
            return False

        if len(vectors) != len(metadatas):
            msg = "vectors and metadatas must have the same length"
            raise ValueError(msg)
        if ids and len(ids) != len(vectors):
            msg = "ids must have the same length as vectors"
            raise ValueError(msg)

        final_ids = list(ids) if ids else [self._extract_id(m) for m in metadatas]
        points = self._build_points(vectors, metadatas, final_ids)

        with _get_tracer().start_as_current_span("vector.upsert") as span:
            span.set_attribute(VECTOR_OPERATION, "upsert")
            t0 = time.perf_counter()
            try:
                for start in range(0, len(points), _UPSERT_CHUNK_SIZE):
                    result = self._client.upsert(
                        collection_name=self._collection_name,
                        points=points[start : start + _UPSERT_CHUNK_SIZE],
                        wait=wait,
                    )
                    _require_write_acknowledgement(result, wait=wait, operation="upsert")
                elapsed = time.perf_counter() - t0
                record_db_query("qdrant_upsert", elapsed)
                record_vector_write(operation="upsert", status="success")
                span.set_attribute(VECTOR_STATUS, "success")
                return True
            except Exception as exc:
                elapsed = time.perf_counter() - t0
                record_db_query("qdrant_upsert", elapsed)
                logger.error(
                    "vector_upsert_failed", extra={"count": len(vectors), "error": str(exc)}
                )
                record_vector_write(operation="upsert", status="failed")
                span.set_attribute(VECTOR_STATUS, "error")
                if self._required:
                    raise VectorStoreError(str(exc)) from exc
                self._available = False
                return False

    def replace_request_notes(
        self,
        request_id: int | str,
        vectors: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, Any]],
        ids: Sequence[str] | None = None,
        *,
        wait: bool = True,
    ) -> bool:
        """Replace a request's points (upsert new, delete stale).

        ``wait=False`` accepts Qdrant's ``acknowledged`` status; callers that
        persist an indexed timestamp keep the default ``wait=True`` and require
        ``completed``.
        """
        if not self._available:
            self.ensure_available()
        if not self._available:
            logger.warning(
                "vector_replace_skipped",
                extra={"reason": "not_available", "request_id": request_id, "count": len(vectors)},
            )
            return False

        if len(vectors) != len(metadatas):
            msg = "vectors and metadatas must have the same length"
            raise ValueError(msg)
        if ids and len(ids) != len(vectors):
            msg = "ids must have the same length as vectors"
            raise ValueError(msg)

        final_ids = list(ids) if ids else [self._extract_id(m) for m in metadatas]
        new_uuid_strs = {_str_to_uuid(raw_id) for raw_id in final_ids}
        points = self._build_points(vectors, metadatas, final_ids)

        client = self._client
        with _get_tracer().start_as_current_span("vector.replace") as span:
            span.set_attribute(VECTOR_OPERATION, "replace")
            t0 = time.perf_counter()
            try:
                existing_uuid_strs = self._fetch_request_point_ids(request_id)
                for start in range(0, len(points), _UPSERT_CHUNK_SIZE):
                    result = client.upsert(
                        collection_name=self._collection_name,
                        points=points[start : start + _UPSERT_CHUNK_SIZE],
                        wait=wait,
                    )
                    _require_write_acknowledgement(result, wait=wait, operation="replace upsert")
                stale = existing_uuid_strs - new_uuid_strs
                if stale:
                    result = client.delete(
                        collection_name=self._collection_name,
                        points_selector=PointIdsList(points=list(stale)),
                        wait=wait,
                    )
                    _require_write_acknowledgement(result, wait=wait, operation="replace delete")
                elapsed = time.perf_counter() - t0
                record_db_query("qdrant_replace", elapsed)
                record_vector_write(operation="replace", status="success")
                span.set_attribute(VECTOR_STATUS, "success")
                return True
            except Exception as exc:
                elapsed = time.perf_counter() - t0
                record_db_query("qdrant_replace", elapsed)
                logger.error(
                    "vector_replace_failed",
                    extra={"request_id": request_id, "count": len(vectors), "error": str(exc)},
                )
                record_vector_write(operation="replace", status="failed")
                span.set_attribute(VECTOR_STATUS, "error")
                if self._required:
                    raise VectorStoreError(str(exc)) from exc
                self._available = False
                return False

    def replace_summary_point(
        self,
        request_id: int | str,
        raw_id: str,
        vector: Sequence[float],
        payload: dict[str, Any],
        *,
        wait: bool = True,
    ) -> bool:
        """Replace a request's single summary point, writing ``payload`` VERBATIM.

        The read-your-writes fast-path (ADR-0012) uses this instead of
        :meth:`replace_request_notes` because ``payload`` must be byte-identical
        to the shared point shape (:mod:`app.infrastructure.vector.summary_point`):
        no empty-list pruning and no scope overwrite (``_build_points`` would do
        both), so the reconciler sees no drift.

        Stale orphan removal: delete all points for ``request_id`` by filter
        before upserting the new point. This eliminates the TOCTOU race present
        in the previous fetch-stale-ids-then-delete approach (a concurrent writer
        between the scroll and the delete could produce orphans that the
        filter-delete avoids). Any transient gap between delete and upsert is
        closed by the 30-minute reconciler (ADR-0012). Fully synchronous --
        callers wrap it in ``asyncio.to_thread``.
        """
        if not self._available:
            self.ensure_available()
        if not self._available:
            logger.warning(
                "vector_replace_skipped",
                extra={"reason": "not_available", "request_id": request_id, "count": 1},
            )
            return False

        point_uuid = _str_to_uuid(raw_id)
        point = PointStruct(id=point_uuid, vector=list(vector), payload=dict(payload))

        client = self._client
        with _get_tracer().start_as_current_span("vector.replace") as span:
            span.set_attribute(VECTOR_OPERATION, "replace")
            t0 = time.perf_counter()
            try:
                # Delete all existing points for this request_id by filter so
                # that a re-summarization (new summary_id) leaves no orphan.
                # Filter-delete avoids the fetch-then-delete TOCTOU; the
                # subsequent upsert writes the new point.
                delete_result = client.delete(
                    collection_name=self._collection_name,
                    points_selector=FilterSelector(
                        filter=Filter(
                            must=[
                                FieldCondition(
                                    key="request_id",
                                    match=MatchValue(value=int(request_id)),
                                )
                            ]
                        )
                    ),
                    wait=wait,
                )
                _require_write_acknowledgement(
                    delete_result, wait=wait, operation="summary replace delete"
                )
                upsert_result = client.upsert(
                    collection_name=self._collection_name,
                    points=[point],
                    wait=wait,
                )
                _require_write_acknowledgement(
                    upsert_result, wait=wait, operation="summary replace upsert"
                )
                elapsed = time.perf_counter() - t0
                record_db_query("qdrant_replace", elapsed)
                record_vector_write(operation="replace", status="success")
                span.set_attribute(VECTOR_STATUS, "success")
                return True
            except Exception as exc:
                elapsed = time.perf_counter() - t0
                record_db_query("qdrant_replace", elapsed)
                logger.error(
                    "vector_replace_summary_failed",
                    extra={"request_id": request_id, "error": str(exc)},
                )
                record_vector_write(operation="replace", status="failed")
                span.set_attribute(VECTOR_STATUS, "error")
                if self._required:
                    raise VectorStoreError(str(exc)) from exc
                self._available = False
                return False

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def query(
        self,
        query_vector: Sequence[float],
        filters: dict[str, Any] | None,
        top_k: int,
    ) -> VectorQueryResult:
        if not self._available:
            self.ensure_available()
        if not self._available:
            logger.warning(
                "vector_query_skipped", extra={"reason": "not_available", "top_k": top_k}
            )
            return VectorQueryResult.empty()

        if top_k <= 0:
            msg = "top_k must be positive"
            raise ValueError(msg)

        filter_payload = {
            key: value
            for key, value in (filters or {}).items()
            if key not in {"environment", "user_scope"}
        }
        qdrant_filter = QdrantQueryFilters(
            environment=self._environment,
            user_scope=self._user_scope,
            **filter_payload,
        ).to_filter()

        with _get_tracer().start_as_current_span("vector.query") as span:
            span.set_attribute(VECTOR_OPERATION, "query")
            t0 = time.perf_counter()
            try:
                client = self._client
                response = client.query_points(
                    collection_name=self._collection_name,
                    query=list(query_vector),
                    query_filter=qdrant_filter,
                    limit=top_k,
                    with_payload=True,
                )
                elapsed = time.perf_counter() - t0
                record_db_query("qdrant_query", elapsed)
                # Qdrant COSINE returns similarity (1=identical).
                # Convert to distance convention: distance = 1 - similarity.
                hits = [
                    VectorQueryHit(
                        id=str(p.id),
                        distance=max(0.0, 1.0 - float(p.score)),
                        metadata=dict(p.payload or {}),
                    )
                    for p in response.points
                ]
                span.set_attribute(VECTOR_STATUS, "success")
                return VectorQueryResult(hits=hits)
            except Exception as exc:
                elapsed = time.perf_counter() - t0
                record_db_query("qdrant_query", elapsed)
                logger.error("vector_query_failed", extra={"error": str(exc)})
                span.set_attribute(VECTOR_STATUS, "error")
                if self._required:
                    raise VectorStoreError(str(exc)) from exc
                self._available = False
                return VectorQueryResult.empty()

    def query_filter(
        self,
        query_vector: Sequence[float],
        qdrant_filter: Any,
        top_k: int,
        *,
        score_threshold: float | None = None,
    ) -> VectorQueryResult:
        """Query with a caller-supplied native Qdrant ``Filter``.

        Absorbs the ``_client`` / ``_collection_name`` private bypass that the
        repository and git-mirror search services used to hand-roll: the caller
        (the unified retrieval adapter) builds the scope-checked filter, while
        this method keeps the client call, ``score_threshold`` semantics, and
        the ``distance = 1 - similarity`` convention in one place.
        """
        if not self._available:
            self.ensure_available()
        if not self._available:
            logger.warning(
                "vector_query_skipped", extra={"reason": "not_available", "top_k": top_k}
            )
            return VectorQueryResult.empty()
        if top_k <= 0:
            msg = "top_k must be positive"
            raise ValueError(msg)

        with _get_tracer().start_as_current_span("vector.query_filter") as span:
            span.set_attribute(VECTOR_OPERATION, "query_filter")
            t0 = time.perf_counter()
            try:
                response = self._client.query_points(
                    collection_name=self._collection_name,
                    query=list(query_vector),
                    query_filter=qdrant_filter,
                    limit=top_k,
                    score_threshold=score_threshold,
                    with_payload=True,
                )
                record_db_query("qdrant_query", time.perf_counter() - t0)
                hits = [
                    VectorQueryHit(
                        id=str(p.id),
                        distance=max(0.0, 1.0 - float(p.score)),
                        metadata=dict(p.payload or {}),
                    )
                    for p in response.points
                ]
                span.set_attribute(VECTOR_STATUS, "success")
                return VectorQueryResult(hits=hits)
            except Exception as exc:
                record_db_query("qdrant_query", time.perf_counter() - t0)
                logger.error("vector_query_filter_failed", extra={"error": str(exc)})
                span.set_attribute(VECTOR_STATUS, "error")
                if self._required:
                    raise VectorStoreError(str(exc)) from exc
                self._available = False
                return VectorQueryResult.empty()

    def find_similar_by_id(
        self,
        point_id: str,
        qdrant_filter: Any,
        top_k: int,
        *,
        score_threshold: float | None = None,
    ) -> VectorQueryResult:
        """Recommend points nearest to the stored vector of ``point_id``.

        Qdrant resolves ``point_id`` to its indexed vector and searches without
        a re-embed. The seed MUST be excluded by the caller via a ``must_not``
        ``HasIdCondition`` in ``qdrant_filter`` (the retrieval adapter adds it)
        so a point never appears in its own similar set.
        """
        if not self._available:
            self.ensure_available()
        if not self._available:
            logger.warning(
                "vector_query_skipped",
                extra={"reason": "not_available", "point_id": point_id},
            )
            return VectorQueryResult.empty()
        if top_k <= 0:
            msg = "top_k must be positive"
            raise ValueError(msg)

        with _get_tracer().start_as_current_span("vector.find_similar_by_id") as span:
            span.set_attribute(VECTOR_OPERATION, "find_similar_by_id")
            t0 = time.perf_counter()
            try:
                response = self._client.query_points(
                    collection_name=self._collection_name,
                    query=point_id,
                    query_filter=qdrant_filter,
                    limit=top_k,
                    score_threshold=score_threshold,
                    with_payload=True,
                )
                record_db_query("qdrant_query", time.perf_counter() - t0)
                hits = [
                    VectorQueryHit(
                        id=str(p.id),
                        distance=max(0.0, 1.0 - float(p.score)),
                        metadata=dict(p.payload or {}),
                    )
                    for p in response.points
                ]
                span.set_attribute(VECTOR_STATUS, "success")
                return VectorQueryResult(hits=hits)
            except Exception as exc:
                record_db_query("qdrant_query", time.perf_counter() - t0)
                logger.error("vector_find_similar_failed", extra={"error": str(exc)})
                span.set_attribute(VECTOR_STATUS, "error")
                if self._required:
                    raise VectorStoreError(str(exc)) from exc
                self._available = False
                return VectorQueryResult.empty()

    def delete_by_request_id(self, request_id: int | str) -> None:
        self.delete_by_request_ids([request_id])

    def delete_by_request_ids(self, request_ids: Sequence[int | str]) -> None:
        """Delete vectors for request IDs using bounded ``MatchAny`` filters."""
        deduped_ids = list(dict.fromkeys(int(request_id) for request_id in request_ids))
        if not deduped_ids:
            return
        if not self._available:
            self.ensure_available()
        if not self._available:
            logger.warning(
                "vector_delete_skipped",
                extra={"reason": "not_available", "request_count": len(deduped_ids)},
            )
            return
        try:
            for start in range(0, len(deduped_ids), _DELETE_FILTER_CHUNK_SIZE):
                chunk = deduped_ids[start : start + _DELETE_FILTER_CHUNK_SIZE]
                self._client.delete(
                    collection_name=self._collection_name,
                    points_selector=FilterSelector(
                        filter=Filter(
                            must=[
                                FieldCondition(
                                    key="request_id",
                                    match=MatchAny(any=chunk),
                                )
                            ]
                        )
                    ),
                    wait=True,
                )
        except Exception as exc:
            logger.error(
                "vector_delete_failed",
                extra={"request_count": len(deduped_ids), "error": str(exc)},
            )
            record_vector_write(operation="delete", status="failed")
            if self._required:
                raise VectorStoreError(str(exc)) from exc
            self._available = False

    def health_check(self) -> bool:
        if not self._available or self._client is None:
            return False
        try:
            self._client.get_collections()
            return True
        except Exception:
            self._available = False
            return False

    def reset(self) -> None:
        client = self._client
        try:
            client.delete_collection(self._collection_name)
            client.create_collection(
                collection_name=self._collection_name,
                vectors_config=VectorParams(size=self._embedding_dim, distance=Distance.COSINE),
            )
        except Exception as exc:
            logger.error("vector_reset_failed", extra={"error": str(exc)})
            raise

    def count(self) -> int:
        if not self._available:
            self.ensure_available()
        if not self._available:
            return 0
        try:
            result = self._client.count(
                collection_name=self._collection_name,
                exact=True,
            )
            return result.count
        except Exception:
            return 0

    def close(self) -> None:
        client = getattr(self, "_client", None)
        if client is None:
            return
        try:
            client.close()
        except Exception as exc:
            logger.warning("vector_client_close_failed", extra={"error": str(exc)})
        finally:
            self._client = None
            self._available = False

    async def aclose(self) -> None:
        try:
            await asyncio.to_thread(self.close)
        except Exception:
            logger.exception("vector_client_async_close_failed")
