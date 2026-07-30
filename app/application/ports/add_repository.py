"""Ports used when adding a repository to the system.

Both are satisfied by thin adapters over machinery that already exists — the
GitHub platform extractor and the git-mirror repository — so
:class:`~app.application.use_cases.add_repository.AddRepositoryUseCase` can
orchestrate metadata ingest, starring and backup enrollment without importing
the adapter or persistence layers.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RepositoryIngestPort(Protocol):
    """Fetch a GitHub repository's metadata and store it locally."""

    async def ingest(
        self,
        *,
        url: str,
        user_id: int,
        correlation_id: str,
    ) -> int:
        """Ingest the repository at *url* and return its local ``repositories.id``.

        Raises the GitHub domain errors (integration required, auth, not found)
        rather than swallowing them; the caller maps them to transport codes.
        """
        ...


@runtime_checkable
class MirrorEnrollmentPort(Protocol):
    """Register a repository for on-disk git mirroring."""

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
        """Upsert a mirror target for ``owner/name`` and return its row id.

        Nothing is cloned here: the next git-backup run picks the row up. The
        default ``pinned=True`` reflects the only caller — an explicit request
        from the user, which reconciliation must not undo.
        """
        ...
