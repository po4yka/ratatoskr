"""Adapter: enroll a single GitHub repository for on-disk mirroring.

The bulk path (``app.tasks.git_backup_sync``) enrolls whole categories on a cron.
This is the per-repository entry point used when the user adds one deliberately,
so the row is pinned: reconciliation infers which mirrors to drop, and it must not
infer its way into dropping a backup that was asked for by name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.adapters.git_backup.repository import GitMirrorRepository

logger = get_logger(__name__)


class GitMirrorEnrollmentAdapter:
    """Upsert one ``git_mirrors`` row for a GitHub repository."""

    def __init__(self, mirror_repo: GitMirrorRepository) -> None:
        self._mirror_repo = mirror_repo

    async def enroll(
        self,
        *,
        user_id: int,
        owner: str,
        name: str,
        repository_id: int,
        size_kb: int | None = None,
        pinned: bool = True,
    ) -> int:
        """Register ``owner/name`` as a mirror target and return the row id."""
        from app.core.git_url_safety import assert_safe_git_url
        from app.db.models.git_backup import GitMirrorSource

        full_name = f"{owner}/{name}"
        clone_url = f"https://github.com/{full_name}.git"

        # The URL is built from names GitHub itself returned, so this cannot
        # normally fail; kept as defence in depth alongside the resolution-time
        # check the mirror worker runs immediately before cloning.
        assert_safe_git_url(clone_url)

        row = await self._mirror_repo.upsert_target(
            user_id=user_id,
            source=GitMirrorSource.GITHUB,
            clone_url=clone_url,
            name=full_name,
            repository_id=repository_id,
            size_kb=size_kb,
            pinned=pinned,
        )
        logger.info(
            "git_mirror_enrolled_for_repository",
            extra={"user_id": user_id, "full_name": full_name, "mirror_id": row.id},
        )
        return int(row.id)
