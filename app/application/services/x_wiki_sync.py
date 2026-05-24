"""Field Theory wiki delta-sync into the shared Qdrant collection.

The x_bookmarks wiki is a directory of markdown pages produced by `ft`.
Per the integration design (`docs/explanation/x-bookmarks-integration.md`),
Qdrant is the wiki's sole persistence beyond the source filesystem -- there
is no Postgres mirror. This service walks the on-disk library, content-hashes
each page, and upserts changed pages as ``entity_type="x_wiki"``
points; paths that disappear from disk become hard deletes.
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.core.logging_utils import get_logger
from app.core.uuid_utils import str_to_uuid

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.application.ports.search import EmbeddingProviderPort

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WikiSyncSummary:
    """Counts emitted by one ``XWikiSyncService.sync()`` invocation."""

    files_seen: int = 0
    files_changed: int = 0
    files_skipped: int = 0
    orphans_deleted: int = 0


def x_wiki_point_id(path: pathlib.Path) -> str:
    """Deterministic Qdrant point UUID for a wiki page.

    Derived from the absolute path so the same file always hashes to the
    same point; content edits overwrite the existing point rather than
    spawning duplicates.
    """
    return str_to_uuid(str(path.absolute()))


@runtime_checkable
class _WikiVectorStorePort(Protocol):
    """Subset of ``QdrantVectorStore`` consumed by this service.

    Defined inline (not in ``app/application/ports``) because no other
    application-layer caller needs this view of the vector store today.
    """

    def get_indexed_x_wiki_path_hashes(
        self, *, user_id: int | None = ..., limit: int | None = ...
    ) -> dict[str, str]: ...

    def upsert_notes(
        self,
        vectors: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, object]],
        ids: Sequence[str] | None = ...,
    ) -> None: ...

    def delete_x_wiki_paths(self, wiki_paths: Sequence[str]) -> None: ...


class XWikiSyncService:
    """Walk the wiki library, embed changed pages, delete orphan points.

    Decision (see ``mem-1779545237-7a8b`` precedent): the drift check uses
    the sibling ``get_indexed_x_wiki_path_hashes`` helper on the
    store so the service body stays thin and reuses the same filter shape
    as the existing ``get_indexed_x_wiki_paths`` reader; the
    set-returning helper is left untouched for callers that only need the
    path set.
    """

    def __init__(
        self,
        *,
        library_path: pathlib.Path | str,
        vector_store: _WikiVectorStorePort,
        embedding_service: EmbeddingProviderPort,
        user_id: int | None = None,
    ) -> None:
        self._library_path = pathlib.Path(library_path)
        self._vector_store = vector_store
        self._embedding_service = embedding_service
        self._user_id = user_id

    async def sync(self) -> WikiSyncSummary:
        """One delta-scan pass: walk + hash + embed-on-change + orphan delete."""
        if not self._library_path.exists() or not self._library_path.is_dir():
            logger.warning(
                "x_wiki_sync_library_missing",
                extra={"library_path": str(self._library_path)},
            )
            return WikiSyncSummary()

        indexed = self._vector_store.get_indexed_x_wiki_path_hashes(user_id=self._user_id)

        files_seen = 0
        files_changed = 0
        files_skipped = 0
        current_paths: set[str] = set()

        for path in sorted(self._library_path.glob("*.md")):
            if not path.is_file():
                continue
            files_seen += 1
            absolute = str(path.absolute())
            current_paths.add(absolute)

            try:
                body = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "x_wiki_sync_read_failed",
                    extra={"wiki_path": absolute, "error": str(exc)},
                )
                continue

            content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            existing_hash = indexed.get(absolute)
            if existing_hash == content_hash:
                files_skipped += 1
                logger.debug(
                    "x_wiki_sync_file_skipped",
                    extra={"wiki_path": absolute},
                )
                continue

            embedding = await self._embedding_service.generate_embedding(
                body,
                task_type="document",
            )
            payload: dict[str, object] = {
                "entity_type": "x_wiki",
                "wiki_path": absolute,
                "content_hash": content_hash,
            }
            self._vector_store.upsert_notes(
                vectors=[embedding],
                metadatas=[payload],
                ids=[absolute],
            )
            files_changed += 1
            logger.info(
                "x_wiki_sync_file_indexed",
                extra={"wiki_path": absolute, "content_hash": content_hash},
            )

        orphan_paths = sorted(set(indexed.keys()) - current_paths)
        if orphan_paths:
            self._vector_store.delete_x_wiki_paths(orphan_paths)
            for orphan in orphan_paths:
                logger.info(
                    "x_wiki_sync_orphan_deleted",
                    extra={"wiki_path": orphan},
                )

        summary = WikiSyncSummary(
            files_seen=files_seen,
            files_changed=files_changed,
            files_skipped=files_skipped,
            orphans_deleted=len(orphan_paths),
        )
        logger.info(
            "x_wiki_sync_completed",
            extra={
                "files_seen": summary.files_seen,
                "files_changed": summary.files_changed,
                "files_skipped": summary.files_skipped,
                "orphans_deleted": summary.orphans_deleted,
            },
        )
        return summary


__all__ = [
    "WikiSyncSummary",
    "XWikiSyncService",
    "x_wiki_point_id",
]
