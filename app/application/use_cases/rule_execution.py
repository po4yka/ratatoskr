"""Automation rule evaluation and execution use case."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Protocol

from app.application.dto.rule_execution import (
    RuleActionResultDTO,
    RuleEvaluationContextDTO,
    RuleExecutionResultDTO,
)
from app.application.use_cases._tracing import use_case_span
from app.core.logging_utils import get_logger
from app.domain.services.rule_engine import MAX_EXECUTIONS_PER_MINUTE, RuleConditionEvaluator
from app.domain.services.tag_service import normalize_tag_name

if TYPE_CHECKING:
    from app.application.ports.rules import (
        CollectionMembershipPort,
        RuleContextPort,
        RuleRateLimiterPort,
        RuleRepositoryPort,
        WebhookDispatchPort,
    )
    from app.application.ports.summaries import SummaryRepositoryPort, TagRepositoryPort

logger = get_logger(__name__)

RULE_WINDOW_SECONDS = 60.0


class RuleActionHandler(Protocol):
    """Strategy protocol for a single automation-rule action type."""

    action_type: str

    async def execute(
        self,
        *,
        action: dict[str, Any],
        user_id: int,
        summary_id: int | None,
        event_type: str,
        event_data: dict[str, Any],
        context: RuleEvaluationContextDTO,
    ) -> RuleActionResultDTO: ...


@dataclass(slots=True)
class _AddTagActionHandler:
    tag_repository: TagRepositoryPort
    action_type: str = "add_tag"

    async def execute(
        self,
        *,
        action: dict[str, Any],
        user_id: int,
        summary_id: int | None,
        event_type: str,
        event_data: dict[str, Any],
        context: RuleEvaluationContextDTO,
    ) -> RuleActionResultDTO:
        del event_type, event_data, context
        if summary_id is None:
            return RuleActionResultDTO(
                type=self.action_type, success=False, detail="skipped: no summary_id"
            )
        params = action.get("params") or {}
        normalized_name = normalize_tag_name(params.get("tag_name", ""))
        if not normalized_name:
            return RuleActionResultDTO(
                type=self.action_type, success=False, detail="skipped: empty tag name"
            )

        tag = await self.tag_repository.async_get_tag_by_normalized_name(user_id, normalized_name)
        if tag is None:
            tag = await self.tag_repository.async_create_tag(
                user_id=user_id,
                name=str(params.get("tag_name", "")).strip(),
                normalized_name=normalized_name,
                color=None,
            )
        await self.tag_repository.async_attach_tag(summary_id, tag["id"], "rule")
        return RuleActionResultDTO(
            type=self.action_type, success=True, detail=f"tag '{normalized_name}' added"
        )


@dataclass(slots=True)
class _RemoveTagActionHandler:
    tag_repository: TagRepositoryPort
    action_type: str = "remove_tag"

    async def execute(
        self,
        *,
        action: dict[str, Any],
        user_id: int,
        summary_id: int | None,
        event_type: str,
        event_data: dict[str, Any],
        context: RuleEvaluationContextDTO,
    ) -> RuleActionResultDTO:
        del event_type, event_data, context
        if summary_id is None:
            return RuleActionResultDTO(
                type=self.action_type, success=False, detail="skipped: no summary_id"
            )
        params = action.get("params") or {}
        normalized_name = normalize_tag_name(params.get("tag_name", ""))
        if not normalized_name:
            return RuleActionResultDTO(
                type=self.action_type, success=False, detail="skipped: empty tag name"
            )

        tag = await self.tag_repository.async_get_tag_by_normalized_name(user_id, normalized_name)
        if tag is None:
            return RuleActionResultDTO(
                type=self.action_type, success=False, detail=f"tag '{normalized_name}' not found"
            )
        await self.tag_repository.async_detach_tag(summary_id, tag["id"])
        return RuleActionResultDTO(
            type=self.action_type, success=True, detail=f"tag '{normalized_name}' removed"
        )


@dataclass(slots=True)
class _AddToCollectionActionHandler:
    collection_membership: CollectionMembershipPort
    action_type: str = "add_to_collection"

    async def execute(
        self,
        *,
        action: dict[str, Any],
        user_id: int,
        summary_id: int | None,
        event_type: str,
        event_data: dict[str, Any],
        context: RuleEvaluationContextDTO,
    ) -> RuleActionResultDTO:
        del event_type, event_data, context
        if summary_id is None:
            return RuleActionResultDTO(
                type=self.action_type, success=False, detail="skipped: no summary_id"
            )
        params = action.get("params") or {}
        collection_id = params.get("collection_id")
        if collection_id is None:
            return RuleActionResultDTO(
                type=self.action_type, success=False, detail="skipped: no collection_id"
            )
        detail = await self.collection_membership.async_add_summary(
            user_id=user_id,
            collection_id=int(collection_id),
            summary_id=summary_id,
        )
        return RuleActionResultDTO(type=self.action_type, success=True, detail=detail)


@dataclass(slots=True)
class _RemoveFromCollectionActionHandler:
    collection_membership: CollectionMembershipPort
    action_type: str = "remove_from_collection"

    async def execute(
        self,
        *,
        action: dict[str, Any],
        user_id: int,
        summary_id: int | None,
        event_type: str,
        event_data: dict[str, Any],
        context: RuleEvaluationContextDTO,
    ) -> RuleActionResultDTO:
        del event_type, event_data, context
        if summary_id is None:
            return RuleActionResultDTO(
                type=self.action_type, success=False, detail="skipped: no summary_id"
            )
        params = action.get("params") or {}
        collection_id = params.get("collection_id")
        if collection_id is None:
            return RuleActionResultDTO(
                type=self.action_type, success=False, detail="skipped: no collection_id"
            )
        detail = await self.collection_membership.async_remove_summary(
            user_id=user_id,
            collection_id=int(collection_id),
            summary_id=summary_id,
        )
        return RuleActionResultDTO(type=self.action_type, success=True, detail=detail)


@dataclass(slots=True)
class _ArchiveActionHandler:
    summary_repository: SummaryRepositoryPort
    action_type: str = "archive"

    async def execute(
        self,
        *,
        action: dict[str, Any],
        user_id: int,
        summary_id: int | None,
        event_type: str,
        event_data: dict[str, Any],
        context: RuleEvaluationContextDTO,
    ) -> RuleActionResultDTO:
        del action, event_type, event_data, context
        if summary_id is None:
            return RuleActionResultDTO(
                type=self.action_type, success=False, detail="skipped: no summary_id"
            )
        found = await self.summary_repository.async_soft_delete_summary_for_user(
            summary_id, user_id
        )
        if not found:
            return RuleActionResultDTO(
                type=self.action_type,
                success=False,
                detail="skipped: summary not found or not owned by user",
            )
        return RuleActionResultDTO(type=self.action_type, success=True, detail="archived")


@dataclass(slots=True)
class _SetFavoriteActionHandler:
    summary_repository: SummaryRepositoryPort
    action_type: str = "set_favorite"

    async def execute(
        self,
        *,
        action: dict[str, Any],
        user_id: int,
        summary_id: int | None,
        event_type: str,
        event_data: dict[str, Any],
        context: RuleEvaluationContextDTO,
    ) -> RuleActionResultDTO:
        del event_type, event_data, context
        if summary_id is None:
            return RuleActionResultDTO(
                type=self.action_type, success=False, detail="skipped: no summary_id"
            )
        value = bool((action.get("params") or {}).get("value", True))
        found = await self.summary_repository.async_set_favorite_for_user(
            summary_id, user_id, value
        )
        if not found:
            return RuleActionResultDTO(
                type=self.action_type,
                success=False,
                detail="skipped: summary not found or not owned by user",
            )
        return RuleActionResultDTO(
            type=self.action_type, success=True, detail=f"is_favorited set to {value}"
        )


@dataclass(slots=True)
class _SendWebhookActionHandler:
    webhook_dispatcher: WebhookDispatchPort
    action_type: str = "send_webhook"

    async def execute(
        self,
        *,
        action: dict[str, Any],
        user_id: int,
        summary_id: int | None,
        event_type: str,
        event_data: dict[str, Any],
        context: RuleEvaluationContextDTO,
    ) -> RuleActionResultDTO:
        params = action.get("params") or {}
        url = str(params.get("url", "")).strip()
        if not url:
            return RuleActionResultDTO(
                type=self.action_type, success=False, detail="skipped: empty url"
            )
        status_code = await self.webhook_dispatcher.async_dispatch(
            url,
            {
                "event_type": event_type,
                "user_id": user_id,
                "summary_id": summary_id,
                "event_data": event_data,
                "context": context.as_dict(),
            },
        )
        return RuleActionResultDTO(
            type=self.action_type, success=True, detail=f"webhook status {status_code}"
        )


class RuleExecutionUseCase:
    """Evaluate matching automation rules and dispatch their actions."""

    def __init__(
        self,
        *,
        rule_repository: RuleRepositoryPort,
        tag_repository: TagRepositoryPort,
        summary_repository: SummaryRepositoryPort,
        collection_membership: CollectionMembershipPort,
        rule_context: RuleContextPort,
        webhook_dispatcher: WebhookDispatchPort,
        rate_limiter: RuleRateLimiterPort,
        handlers: list[RuleActionHandler] | None = None,
    ) -> None:
        self._rule_repository = rule_repository
        self._tag_repository = tag_repository
        self._summary_repository = summary_repository
        self._collection_membership = collection_membership
        self._rule_context = rule_context
        self._webhook_dispatcher = webhook_dispatcher
        self._rate_limiter = rate_limiter
        handler_list = handlers or self._build_default_handlers()
        self._handlers = {handler.action_type: handler for handler in handler_list}

    async def evaluate_and_execute(
        self,
        user_id: int,
        event_type: str,
        event_data: dict[str, Any],
        processing_rule_ids: set[int] | None = None,
    ) -> list[RuleExecutionResultDTO]:
        """Evaluate enabled rules for an event and execute the matched actions."""
        with use_case_span(
            "rule_execution.evaluate_and_execute",
            user_id=user_id,
            attributes={"ratatoskr.rule.event_type": event_type},
        ):
            if processing_rule_ids is None:
                processing_rule_ids = set()

            allowed = await self._rate_limiter.async_allow_execution(
                user_id,
                limit=MAX_EXECUTIONS_PER_MINUTE,
                window_seconds=RULE_WINDOW_SECONDS,
            )
            if not allowed:
                logger.warning(
                    "rule_execution_rate_limited",
                    extra={"user_id": user_id, "event_type": event_type},
                )
                return []

            rules = await self._rule_repository.async_get_rules_by_event_type(user_id, event_type)
            if not rules:
                return []

            context_dto = await self._rule_context.async_build_context(event_data)
            context = context_dto.as_dict()
            summary_id = event_data.get("summary_id")
            results: list[RuleExecutionResultDTO] = []

            for rule in rules:
                rule_id = int(rule["id"])
                if rule_id in processing_rule_ids:
                    logger.warning(
                        "rule_execution_loop_detected",
                        extra={"rule_id": rule_id, "user_id": user_id},
                    )
                    continue

                started_at = time.monotonic()
                error: str | None = None
                matched, conditions_result = RuleConditionEvaluator.evaluate_conditions(
                    rule.get("conditions_json") or [],
                    context,
                    rule.get("match_mode") or "all",
                )
                actions_taken: list[RuleActionResultDTO] = []

                try:
                    if matched:
                        for action in rule.get("actions_json") or []:
                            actions_taken.append(
                                await self._execute_action(
                                    action=action,
                                    user_id=user_id,
                                    summary_id=summary_id,
                                    event_type=event_type,
                                    event_data=event_data,
                                    context=context_dto,
                                )
                            )
                        await self._rule_repository.async_increment_run_count(rule_id)
                except Exception as exc:
                    error = str(exc)
                    logger.exception(
                        "rule_execution_failed",
                        extra={"rule_id": rule_id, "user_id": user_id, "event_type": event_type},
                    )

                duration_ms = int((time.monotonic() - started_at) * 1000)
                await self._rule_repository.async_create_execution_log(
                    rule_id=rule_id,
                    summary_id=summary_id,
                    event_type=event_type,
                    matched=matched,
                    conditions_result=conditions_result,
                    actions_taken=[asdict(action) for action in actions_taken],
                    error=error,
                    duration_ms=duration_ms,
                )
                results.append(
                    RuleExecutionResultDTO(
                        rule_id=rule_id,
                        matched=matched,
                        actions_taken=actions_taken,
                        error=error,
                    )
                )

            return results

    async def _execute_action(
        self,
        *,
        action: dict[str, Any],
        user_id: int,
        summary_id: int | None,
        event_type: str,
        event_data: dict[str, Any],
        context: RuleEvaluationContextDTO,
    ) -> RuleActionResultDTO:
        action_type = str(action.get("type", "")).strip()
        handler = self._handlers.get(action_type)
        if handler is None:
            return RuleActionResultDTO(
                type=action_type,
                success=False,
                detail=f"unknown action: {action_type}",
            )
        try:
            return await handler.execute(
                action=action,
                user_id=user_id,
                summary_id=summary_id,
                event_type=event_type,
                event_data=event_data,
                context=context,
            )
        except Exception as exc:
            logger.exception(
                "rule_action_failed",
                extra={"action_type": action_type, "user_id": user_id, "summary_id": summary_id},
            )
            return RuleActionResultDTO(type=action_type, success=False, detail=str(exc))

    def _build_default_handlers(self) -> list[RuleActionHandler]:
        return [
            _AddTagActionHandler(self._tag_repository),
            _RemoveTagActionHandler(self._tag_repository),
            _AddToCollectionActionHandler(self._collection_membership),
            _RemoveFromCollectionActionHandler(self._collection_membership),
            _ArchiveActionHandler(self._summary_repository),
            _SetFavoriteActionHandler(self._summary_repository),
            _SendWebhookActionHandler(self._webhook_dispatcher),
        ]
