"""Feature gating and staged rollout rules for mixed-source aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.application.ports.users import UserRepositoryPort
    from app.config import AppConfig


class AggregationRolloutStage(StrEnum):
    """Supported rollout stages for the aggregation product surface."""

    DISABLED = "disabled"
    INTERNAL = "internal"
    OWNER_BETA = "owner_beta"
    ENABLED = "enabled"


@dataclass(frozen=True, slots=True)
class AggregationRolloutDecision:
    """One rollout decision for a specific user."""

    enabled: bool
    reason: str
    stage: AggregationRolloutStage


class AggregationRolloutGate:
    """Evaluate whether the aggregation feature is available for one user."""

    def __init__(
        self,
        *,
        cfg: AppConfig,
        user_repo: UserRepositoryPort | None = None,
    ) -> None:
        self._cfg = cfg
        self._user_repo = user_repo

    async def evaluate(self, user_id: int) -> AggregationRolloutDecision:
        """Return rollout availability for the supplied user identifier."""
        stage = AggregationRolloutStage(
            getattr(self._cfg.runtime, "aggregation_rollout_stage", "enabled")
        )
        if not bool(getattr(self._cfg.runtime, "aggregation_bundle_enabled", True)):
            return AggregationRolloutDecision(
                enabled=False,
                reason="Aggregation bundles are disabled in runtime configuration.",
                stage=AggregationRolloutStage.DISABLED,
            )
        if stage == AggregationRolloutStage.DISABLED:
            return AggregationRolloutDecision(
                enabled=False,
                reason="Aggregation bundles are currently disabled.",
                stage=stage,
            )
        if stage == AggregationRolloutStage.ENABLED:
            return AggregationRolloutDecision(
                enabled=True,
                reason="Aggregation bundles are enabled for all authenticated users.",
                stage=stage,
            )
        if stage == AggregationRolloutStage.INTERNAL:
            allowed_ids = tuple(getattr(self._cfg.telegram, "allowed_user_ids", ()))
            enabled = user_id in allowed_ids
            return AggregationRolloutDecision(
                enabled=enabled,
                reason=(
                    "Aggregation bundles are limited to internal allowlisted accounts."
                    if not enabled
                    else "Aggregation bundles are enabled for internal allowlisted accounts."
                ),
                stage=stage,
            )

        is_owner = False
        if self._user_repo is not None:
            user = await self._user_repo.async_get_user_by_telegram_id(user_id)
            is_owner = bool(user and user.get("is_owner"))
        return AggregationRolloutDecision(
            enabled=is_owner,
            reason=(
                "Aggregation bundles are in owner-only beta."
                if not is_owner
                else "Aggregation bundles are enabled for owner beta accounts."
            ),
            stage=stage,
        )
