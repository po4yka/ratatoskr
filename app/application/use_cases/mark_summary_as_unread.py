"""Use case for marking a summary as unread."""

from dataclasses import dataclass
from datetime import datetime

from app.application.ports.summaries import SummaryRepositoryPort
from app.application.use_cases._tracing import use_case_span
from app.application.use_cases.summary_fetch import fetch_summary_or_raise
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.domain.events.summary_events import SummaryMarkedAsUnread
from app.domain.exceptions.domain_exceptions import (
    InvalidStateTransitionError,
)
from app.domain.models.summary import summary_from_dict
from app.domain.services.summary_validator import SummaryValidator

logger = get_logger(__name__)


@dataclass
class MarkSummaryAsUnreadCommand:
    """Command for marking a summary as unread.

    This is an explicit representation of the user's intent.
    """

    summary_id: int
    user_id: int  # For authorization/audit purposes

    def __post_init__(self) -> None:
        if self.summary_id <= 0:
            msg = "summary_id must be positive"
            raise ValueError(msg)
        if self.user_id <= 0:
            msg = "user_id must be positive"
            raise ValueError(msg)


class MarkSummaryAsUnreadUseCase:
    """Use case for marking a summary as unread."""

    def __init__(self, summary_repository: SummaryRepositoryPort) -> None:
        self._summary_repo = summary_repository

    async def execute(self, command: MarkSummaryAsUnreadCommand) -> SummaryMarkedAsUnread:
        """Execute the use case.

        Args:
            command: Command containing the summary ID and user ID.

        Returns:
            Domain event representing the state change.

        Raises:
            ResourceNotFoundError: If summary doesn't exist.
            InvalidStateTransitionError: If summary is already unread.

        """
        with use_case_span("mark_summary_as_unread.execute", command):
            logger.info(
                "mark_summary_as_unread_started",
                extra={"summary_id": command.summary_id, "user_id": command.user_id},
            )

            summary_data = await fetch_summary_or_raise(self._summary_repo, command.summary_id)
            summary = summary_from_dict(summary_data)

            can_mark, reason = SummaryValidator.can_mark_as_unread(summary)
            if not can_mark:
                logger.warning(
                    "mark_summary_as_unread_rejected",
                    extra={
                        "summary_id": command.summary_id,
                        "reason": reason,
                        "is_read": summary.is_read,
                    },
                )
                msg = f"Cannot mark summary as unread: {reason}"
                raise InvalidStateTransitionError(
                    msg,
                    details={
                        "summary_id": command.summary_id,
                        "current_state": "read" if summary.is_read else "unread",
                    },
                )

            try:
                summary.mark_as_unread()
            except ValueError as e:
                logger.exception(
                    "mark_summary_as_unread_domain_error",
                    extra={"summary_id": command.summary_id, "error": str(e)},
                )
                raise InvalidStateTransitionError(
                    str(e),
                    details={"summary_id": command.summary_id},
                ) from e

            await self._summary_repo.async_mark_summary_as_unread(command.summary_id)

            event = SummaryMarkedAsUnread(
                occurred_at=datetime.now(UTC),
                aggregate_id=command.summary_id,
                summary_id=command.summary_id,
            )

            logger.info(
                "mark_summary_as_unread_completed",
                extra={"summary_id": command.summary_id, "user_id": command.user_id},
            )

            return event
