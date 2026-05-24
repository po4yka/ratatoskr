"""Soft-failure escalation policy for LLM summarization retries.

When an LLM call returns HTTP 200 but the response cannot be parsed as
JSON or fails strict-contract validation, retrying the *same* model
usually produces the same broken output. This module captures the
decision of when to "escalate" — advance to the next model in the
tier-specific fallback chain — instead of re-spending the retry
budget on a model that has already demonstrated it cannot handle the
request.

Pure decision module: the policy does not perform the retry itself.
The caller (the instructor retry loop in pure_summary_service.py, or the
OpenRouter chat response handler) consumes the
:class:`EscalationDecision` and either advances the model or falls
back to same-model retry.

A budget cap (``max_escalations``) prevents runaway cost: once
exhausted, further soft failures raise :class:`EscalationBudgetExceeded`
so the caller terminates the request and surfaces an error rather
than chaining indefinitely through expensive ceiling models.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class SoftFailureReason(enum.StrEnum):
    JSON_PARSE_ERROR = "json_parse_error"
    SCHEMA_VALIDATION_ERROR = "schema_validation_error"


@dataclass(frozen=True)
class EscalationDecision:
    advance_model: bool
    next_model: str | None


class EscalationBudgetExceeded(RuntimeError):
    """Raised when the policy has already escalated ``max_escalations`` times."""


class EscalationPolicy:
    """Stateful per-request policy for soft-failure escalation.

    Construct one policy per request (the budget counter is request-scoped).
    Pass ``audit`` (a one-arg callable) to receive structured event payloads
    for observability.
    """

    def __init__(
        self,
        *,
        max_escalations: int = 2,
        audit: Callable[[dict[str, object]], None] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        if max_escalations < 0:
            raise ValueError("max_escalations must be >= 0")
        self._max = max_escalations
        self._audit = audit
        self._correlation_id = correlation_id
        self._used = 0

    @property
    def used_escalations(self) -> int:
        return self._used

    @staticmethod
    def classify_failure(
        *,
        http_status: int,
        json_parse_error: bool,
        schema_validation_error: bool,
    ) -> SoftFailureReason | None:
        """Return the soft-failure reason for a completed call, else None.

        Pure function. Only HTTP-200 responses can be "soft" failures —
        anything else is a hard error handled by the existing retry path.
        """
        if http_status != 200:
            return None
        if json_parse_error:
            return SoftFailureReason.JSON_PARSE_ERROR
        if schema_validation_error:
            return SoftFailureReason.SCHEMA_VALIDATION_ERROR
        return None

    def on_soft_failure(
        self,
        *,
        model: str,
        reason: SoftFailureReason,
        remaining_fallbacks: tuple[str, ...],
    ) -> EscalationDecision:
        """Decide what to do after a soft failure.

        If no fallback remains, the caller should retry same-model (budget
        is not consumed). Otherwise the budget is consumed and the
        next model is returned. Raises :class:`EscalationBudgetExceeded`
        when budget is exhausted but a fallback was offered (signals the
        caller to stop the cascade and return an error).
        """
        if not remaining_fallbacks:
            return EscalationDecision(advance_model=False, next_model=None)

        if self._used >= self._max:
            raise EscalationBudgetExceeded(
                f"escalation budget {self._max} exhausted after {self._used} cascades"
            )

        next_model = remaining_fallbacks[0]
        self._used += 1
        if self._audit is not None:
            self._audit(
                {
                    "event": "llm_soft_failure_escalation",
                    "from_model": model,
                    "to_model": next_model,
                    "reason": reason.value,
                    "used_escalations": self._used,
                    "max_escalations": self._max,
                    "correlation_id": self._correlation_id,
                }
            )
        return EscalationDecision(advance_model=True, next_model=next_model)


__all__ = [
    "EscalationBudgetExceeded",
    "EscalationDecision",
    "EscalationPolicy",
    "SoftFailureReason",
]
