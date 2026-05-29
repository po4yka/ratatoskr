"""Repair-side reconciliation for git-mirror README vectors.

Detection lives in ``GitMirrorVectorIndexedEntityAdapter`` (surfaced in the
reconcile CLI report). This module performs the actual repair, which needs disk
and embedding access and therefore runs in the git_backup worker, not the
read-only diagnostic reconciler:

- Orphaned points (Qdrant ``git_mirror`` points whose row is gone, excluded, or
  now GitHub-linked) are deleted.
- Missing points (rows marked indexed but with no Qdrant point) are recreated by
  re-running the indexer with ``force=True`` (content-hash dedup would otherwise
  skip them); if the bare clone is gone from disk, the row's index columns are
  cleared so a future re-clone re-indexes it.

The whole pass is best-effort: per-item errors are logged and skipped so a
reconcile run can never fail the backup.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from app.core.logging_utils import get_logger
from app.db.models.git_backup import GitMirror, GitMirrorStatus

if TYPE_CHECKING:
    from app.db.session import Database
    from app.infrastructure.search.git_mirror_readme_indexer import GitMirrorReadmeIndexer
    from app.infrastructure.vector.qdrant_store import QdrantVectorStore

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GitMirrorRepairReport:
    """Outcome of one reconcile-and-repair pass."""

    expected: int = 0
    indexed: int = 0
    orphans_deleted: int = 0
    missing_reindexed: int = 0
    missing_cleared: int = 0


class GitMirrorVectorReconciler:
    """Detect and repair drift between ``git_mirrors`` and Qdrant git_mirror points."""

    def __init__(
        self,
        db: Database,
        qdrant_store: QdrantVectorStore | None,
        indexer: GitMirrorReadmeIndexer,
    ) -> None:
        self._db = db
        self._qdrant = qdrant_store
        self._indexer = indexer

    async def reconcile_and_repair(self, *, scan_limit: int = 10_000) -> GitMirrorRepairReport:
        if self._qdrant is None or not self._qdrant.available:
            logger.debug("git_mirror_reconcile_skipped", extra={"reason": "qdrant_unavailable"})
            return GitMirrorRepairReport()

        # Expected: non-GitHub, already indexed, not excluded. Keep mirror_path so
        # missing-point repair can re-index without an extra query.
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(GitMirror.id, GitMirror.mirror_path)
                    .where(
                        GitMirror.repository_id.is_(None),
                        GitMirror.readme_indexed_at.is_not(None),
                        GitMirror.status != GitMirrorStatus.EXCLUDED,
                    )
                    .limit(scan_limit)
                )
            ).all()
        expected_paths: dict[int, str | None] = {row.id: row.mirror_path for row in rows}
        expected_ids = set(expected_paths)

        indexed_ids = await asyncio.to_thread(
            self._qdrant.get_indexed_git_mirror_ids, limit=scan_limit
        )

        orphans = sorted(indexed_ids - expected_ids)
        missing = sorted(expected_ids - indexed_ids)

        orphans_deleted = await self._delete_orphans(orphans)
        reindexed, cleared = await self._repair_missing(missing, expected_paths)

        report = GitMirrorRepairReport(
            expected=len(expected_ids),
            indexed=len(indexed_ids),
            orphans_deleted=orphans_deleted,
            missing_reindexed=reindexed,
            missing_cleared=cleared,
        )
        logger.info(
            "git_mirror_reconcile_done",
            extra={
                "expected": report.expected,
                "indexed": report.indexed,
                "orphans_deleted": report.orphans_deleted,
                "missing_reindexed": report.missing_reindexed,
                "missing_cleared": report.missing_cleared,
            },
        )
        return report

    async def _delete_orphans(self, orphans: list[int]) -> int:
        if not orphans or self._qdrant is None:
            return 0
        try:
            await asyncio.to_thread(self._qdrant.delete_git_mirror_points, orphans)
            return len(orphans)
        except Exception:
            logger.exception("git_mirror_reconcile_orphan_delete_failed", extra={"count": len(orphans)})
            return 0

    async def _repair_missing(
        self, missing: list[int], expected_paths: dict[int, str | None]
    ) -> tuple[int, int]:
        reindexed = 0
        cleared = 0
        for mirror_id in missing:
            try:
                path_str = expected_paths.get(mirror_id)
                if path_str and Path(path_str).exists():
                    mirror = await self._load_mirror(mirror_id)
                    if mirror is not None:
                        await self._indexer.index_mirror(mirror, Path(path_str), force=True)
                        reindexed += 1
                else:
                    await self._clear_index_state(mirror_id)
                    cleared += 1
            except Exception:
                logger.exception(
                    "git_mirror_reconcile_missing_repair_failed", extra={"mirror_id": mirror_id}
                )
        return reindexed, cleared

    async def _load_mirror(self, mirror_id: int) -> GitMirror | None:
        async with self._db.session() as session:
            return await session.get(GitMirror, mirror_id)

    async def _clear_index_state(self, mirror_id: int) -> None:
        async with self._db.transaction() as session:
            await session.execute(
                update(GitMirror)
                .where(GitMirror.id == mirror_id)
                .values(readme_indexed_at=None, readme_content_hash=None)
            )
