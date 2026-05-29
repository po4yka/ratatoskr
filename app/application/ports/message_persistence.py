"""MessagePersistencePort — facade port used by content adapters.

Groups the sub-repository ports that ``MessagePersistence`` exposes so that
content adapters can declare their dependency on an injected port rather than
importing the concrete infrastructure class directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.application.ports.requests import (
        CrawlResultRepositoryPort,
        LLMRepositoryPort,
        RequestRepositoryPort,
    )
    from app.application.ports.users import UserRepositoryPort


@runtime_checkable
class MessagePersistencePort(Protocol):
    """Minimal facade over the persistence sub-repositories used by content adapters."""

    request_repo: RequestRepositoryPort
    user_repo: UserRepositoryPort
    crawl_repo: CrawlResultRepositoryPort
    llm_repo: LLMRepositoryPort

    async def persist_message_snapshot(self, request_id: int, message: Any) -> None:
        """Persist message snapshot to database."""
