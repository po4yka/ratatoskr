"""Reusable workflow helper for handling LLM summary responses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from app.core.logging_utils import get_logger
from app.utils.json_validation import parse_summary_response

from .llm_response_workflow_attempts import LLMWorkflowAttemptsMixin
from .llm_response_workflow_execution import LLMWorkflowExecutionMixin
from .llm_response_workflow_repair import LLMWorkflowRepairMixin
from .llm_response_workflow_storage import LLMWorkflowStorageMixin

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.application.ports.llm_client import LLMClientProtocol
    from app.application.ports.requests import LLMRepositoryPort, RequestRepositoryPort
    from app.application.ports.summaries import SummaryRepositoryPort
    from app.application.ports.users import UserRepositoryPort
    from app.db.session import Database
    from app.db.write_queue import DbWriteQueue

logger = get_logger(__name__)


class ConcurrencyTimeoutError(TimeoutError):
    """Raised when an LLM processing slot cannot be acquired within the timeout."""


class LLMRequestConfig(BaseModel):
    """Configuration for a single LLM attempt."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    preset_name: str | None = None
    messages: list[dict[str, Any]]
    response_format: dict[str, Any]
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    model_override: str | None = None
    fallback_models_override: tuple[str, ...] | None = None
    silent: bool = False
    stream: bool = False
    on_stream_delta: Any | None = None


class LLMRepairContext(BaseModel):
    """Context required to attempt JSON repair prompts."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    base_messages: list[dict[str, Any]]
    repair_response_format: dict[str, Any]
    repair_max_tokens: int | None = None
    default_prompt: str
    missing_fields_prompt: str | None = None


class AttemptContext(BaseModel):
    """Per-attempt bundle passed into workflow attempt processing."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    message: Any
    llm: Any
    req_id: int
    correlation_id: str | None
    interaction_config: Any
    persistence: Any
    repair_context: Any | None = None
    request_config: Any | None = None
    notifications: Any | None = None
    ensure_summary: Any | None = None
    on_success: Any | None = None
    required_summary_fields: tuple[str, ...] = ("tldr", "summary_250", "summary_1000")
    is_last_attempt: bool = False
    failed_attempts: list[tuple[Any, Any]] | None = None
    defer_persistence: bool = False
    call_budget: Any | None = None


class LLMWorkflowNotifications(BaseModel):
    """Notification callbacks invoked during workflow progression."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    completion: Any | None = None
    llm_error: Any | None = None
    repair_failure: Any | None = None
    parsing_failure: Any | None = None
    retry: Any | None = None


class LLMInteractionConfig(BaseModel):
    """Settings for updating user interactions."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    interaction_id: int | None = None
    success_kwargs: dict[str, Any] | None = None
    llm_error_builder: Any | None = None
    repair_failure_kwargs: dict[str, Any] | None = None
    parsing_failure_kwargs: dict[str, Any] | None = None


class LLMSummaryPersistenceSettings(BaseModel):
    """Configuration for persisting summary results."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    lang: str
    is_read: bool = True
    insights_getter: Any | None = None
    defer_write: bool = False


class LLMResponseWorkflow(
    LLMWorkflowExecutionMixin,
    LLMWorkflowAttemptsMixin,
    LLMWorkflowRepairMixin,
    LLMWorkflowStorageMixin,
):
    """Reusable helper encapsulating shared LLM response automation."""

    def __init__(
        self,
        *,
        cfg: Any,
        db: Database,
        response_formatter: ResponseFormatter,
        audit_func: Callable[[str, str, dict[str, Any]], None],
        sem: Callable[[], Any],
        llm_client: LLMClientProtocol | None = None,
        openrouter: LLMClientProtocol | None = None,
        db_write_queue: DbWriteQueue | None = None,
        adaptive_timeout_service: Any | None = None,
        summary_repo: SummaryRepositoryPort | None = None,
        request_repo: RequestRepositoryPort | None = None,
        llm_repo: LLMRepositoryPort | None = None,
        user_repo: UserRepositoryPort | None = None,
    ) -> None:
        """Initialize workflow dependencies and repositories."""
        if llm_client is None:
            llm_client = openrouter
        if llm_client is None:
            msg = "llm_client must be provided by the DI layer"
            raise ValueError(msg)

        self.cfg = cfg
        self.db = db
        self.llm_client = llm_client
        self.openrouter = llm_client
        self.response_formatter = response_formatter
        self._audit = audit_func
        self._sem = sem
        self._db_write_queue = db_write_queue
        self._adaptive_timeout = adaptive_timeout_service
        if summary_repo is None:
            msg = "summary_repo must be provided by the DI layer"
            raise ValueError(msg)
        if request_repo is None:
            msg = "request_repo must be provided by the DI layer"
            raise ValueError(msg)
        if llm_repo is None:
            msg = "llm_repo must be provided by the DI layer"
            raise ValueError(msg)
        if user_repo is None:
            msg = "user_repo must be provided by the DI layer"
            raise ValueError(msg)
        self.summary_repo = summary_repo
        self.request_repo = request_repo
        self.llm_repo = llm_repo
        self.user_repo = user_repo
        self._background_tasks: set[Any] = set()

        try:
            sem_timeout = float(getattr(cfg.runtime, "semaphore_acquire_timeout_sec", 30.0))
            llm_timeout = float(getattr(cfg.runtime, "llm_call_timeout_sec", 180.0))
            if sem_timeout > llm_timeout:
                logger.warning(
                    "timeout_config_suspicious",
                    extra={
                        "semaphore_timeout": sem_timeout,
                        "llm_call_timeout": llm_timeout,
                        "hint": "semaphore timeout should be shorter than LLM call timeout",
                    },
                )
        except (TypeError, ValueError) as exc:
            sem_timeout = 30.0
            llm_timeout = 180.0
            logger.debug(
                "timeout_config_parse_error",
                extra={
                    "error": str(exc),
                    "fallback_semaphore_timeout": sem_timeout,
                    "fallback_llm_timeout": llm_timeout,
                },
            )


__all__ = [
    "AttemptContext",
    "ConcurrencyTimeoutError",
    "LLMInteractionConfig",
    "LLMRepairContext",
    "LLMRequestConfig",
    "LLMResponseWorkflow",
    "LLMSummaryPersistenceSettings",
    "LLMWorkflowNotifications",
    "parse_summary_response",
]
