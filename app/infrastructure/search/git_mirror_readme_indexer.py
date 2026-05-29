"""Semantic indexer for git mirror README content.

Indexes the README of non-GitHub (arbitrary-URL) git mirrors into Qdrant so
they are semantically searchable.  Only mirrors with ``repository_id IS NULL``
are indexed here; GitHub-linked mirrors are already searchable via the
repository-embedding path.

Design notes
------------
- Content-hash dedup: if the SHA-256 hex digest of the extracted README text
  matches the persisted ``readme_content_hash`` on the GitMirror row, the
  embedding + Qdrant upsert are skipped entirely.
- Best-effort: any embedding or Qdrant error is logged and swallowed so
  indexing never fails or blocks the backup sync.
- The indexer does NOT register a VectorIndexedEntityAdapter / reconciler; stale
  points rely on content-hash dedup (no re-embed on unchanged content) plus the
  delete-time cleanup in the DELETE /v1/git-mirrors/{id} endpoint. A full
  reconciler is a follow-up.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.infrastructure.vector.point_ids import git_mirror_point_id

if TYPE_CHECKING:
    from pathlib import Path

    from app.adapters.git_backup.readme_extractor import ReadmeExtractor
    from app.db.models.git_backup import GitMirror
    from app.db.session import Database
    from app.infrastructure.embedding.embedding_protocol import EmbeddingServiceProtocol
    from app.infrastructure.vector.qdrant_store import QdrantVectorStore

logger = get_logger(__name__)


class GitMirrorReadmeIndexer:
    """Embeds and upserts README content for non-GitHub git mirrors into Qdrant.

    Constructor is fully injectable for testing; production callers build from
    the shared DI helpers (``create_embedding_service`` /
    ``build_qdrant_vector_store``).
    """

    def __init__(
        self,
        embedding_service: EmbeddingServiceProtocol,
        qdrant_store: QdrantVectorStore | None,
        db: Database,
        *,
        environment: str,
        user_scope: str,
        readme_extractor: ReadmeExtractor | None = None,
    ) -> None:
        self._embedding_service = embedding_service
        self._qdrant = qdrant_store
        self._db = db
        self._environment = environment
        self._user_scope = user_scope

        if readme_extractor is None:
            from app.adapters.git_backup.readme_extractor import (
                ReadmeExtractor as _ReadmeExtractor,
            )

            self._extractor: ReadmeExtractor = _ReadmeExtractor()
        else:
            self._extractor = readme_extractor

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def index_mirror(self, mirror: GitMirror, bare_repo_path: Path) -> None:
        """Index the README of *mirror* into Qdrant (best-effort, never raises).

        Steps:
        1. Extract README from bare clone via ReadmeExtractor.
        2. Skip if README is empty.
        3. Compute SHA-256 hex digest; skip if equal to persisted hash (dedup).
        4. Embed (task_type="document").
        5. Upsert into Qdrant with entity_type="git_mirror" payload.
        6. Persist readme_content_hash + readme_indexed_at on the GitMirror row.
        """
        try:
            await self._index_mirror_inner(mirror, bare_repo_path)
        except Exception:
            logger.exception(
                "git_mirror_readme_index_failed",
                extra={
                    "mirror_id": mirror.id,
                    "clone_url": mirror.clone_url,
                },
            )

    async def index_mirrors(
        self,
        mirrors: list[tuple[GitMirror, Path]],
    ) -> None:
        """Index multiple mirrors sequentially (best-effort per mirror)."""
        for mirror, path in mirrors:
            await self.index_mirror(mirror, path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _index_mirror_inner(self, mirror: GitMirror, bare_repo_path: Path) -> None:
        # 1. Extract README (blocking I/O — offload to thread).
        readme_text: str = await asyncio.to_thread(self._extractor.extract, bare_repo_path)

        # 2. Skip empty README.
        if not readme_text:
            logger.debug(
                "git_mirror_readme_empty_skip",
                extra={"mirror_id": mirror.id},
            )
            return

        # 3. Content-hash dedup.
        content_hash = hashlib.sha256(readme_text.encode()).hexdigest()
        if mirror.readme_content_hash == content_hash:
            logger.debug(
                "git_mirror_readme_unchanged_skip",
                extra={"mirror_id": mirror.id, "hash": content_hash},
            )
            return

        # 4. Qdrant availability check (fast-path, no network round-trip).
        if self._qdrant is None or not self._qdrant.available:
            logger.debug(
                "git_mirror_readme_qdrant_unavailable_skip",
                extra={"mirror_id": mirror.id},
            )
            return

        # 5. Embed.
        embedding = await self._embedding_service.generate_embedding(
            readme_text,
            language=None,
            task_type="document",
        )
        vector: list[float] = (
            embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
        )

        # 6. Build payload and upsert.
        point_id = git_mirror_point_id(self._environment, self._user_scope, mirror.id)
        payload = self._build_payload(mirror)

        await asyncio.to_thread(
            self._qdrant.upsert_notes,
            [vector],
            [payload],
            [point_id],
        )

        # 7. Persist hash + timestamp on the DB row.
        await self._persist_index_metadata(mirror.id, content_hash)

        logger.info(
            "git_mirror_readme_indexed",
            extra={
                "mirror_id": mirror.id,
                "clone_url": mirror.clone_url,
                "hash": content_hash,
            },
        )

    def _build_payload(self, mirror: GitMirror) -> dict[str, Any]:
        return {
            "entity_type": "git_mirror",
            "mirror_id": mirror.id,
            "user_id": mirror.user_id,
            "name": mirror.name,
            "clone_url": mirror.clone_url,
            "environment": self._environment,
            "user_scope": self._user_scope,
            "language": "en",
        }

    async def _persist_index_metadata(self, mirror_id: int, content_hash: str) -> None:
        import datetime as dt

        from sqlalchemy import update

        from app.db.models.git_backup import GitMirror

        now = dt.datetime.now(tz=dt.UTC)
        async with self._db.transaction() as session:
            await session.execute(
                update(GitMirror)
                .where(GitMirror.id == mirror_id)
                .values(
                    readme_content_hash=content_hash,
                    readme_indexed_at=now,
                )
            )
