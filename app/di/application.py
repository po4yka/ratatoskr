from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.use_cases.get_unread_summaries import GetUnreadSummariesUseCase
from app.application.use_cases.mark_summary_as_read import MarkSummaryAsReadUseCase
from app.application.use_cases.mark_summary_as_unread import MarkSummaryAsUnreadUseCase
from app.application.use_cases.search_topics import SearchTopicsUseCase
from app.di.repositories import build_summary_repository
from app.di.types import ApplicationServices

if TYPE_CHECKING:
    from app.db.session import Database


def build_application_services(
    db: Database,
    *,
    topic_search_service: Any | None = None,
) -> ApplicationServices:
    summary_repository = build_summary_repository(db)
    return ApplicationServices(
        unread_summaries=GetUnreadSummariesUseCase(summary_repository=summary_repository),
        mark_summary_as_read=MarkSummaryAsReadUseCase(summary_repository=summary_repository),
        mark_summary_as_unread=MarkSummaryAsUnreadUseCase(summary_repository=summary_repository),
        search_topics=SearchTopicsUseCase(topic_search_service) if topic_search_service else None,
    )
