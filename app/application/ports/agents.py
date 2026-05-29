"""Agent ports for the application layer.

Application services that orchestrate multi-source extraction, aggregation, and
relationship analysis depend on these Protocols instead of the concrete agent
classes in ``app.agents``.  Concrete agents satisfy these protocols structurally.

Keeping these ports in ``app.application.ports`` lets the service layer remain
free of any ``app.adapters`` dependency (which the concrete agents may carry).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.adapter_models.batch_analysis import (
        RelationshipAnalysisInput,
        RelationshipAnalysisOutput,
    )
    from app.agents.base_agent import AgentResult
    from app.application.dto.aggregation import (
        MultiSourceAggregationOutput,
        MultiSourceExtractionOutput,
    )


@runtime_checkable
class MultiSourceExtractionAgentPort(Protocol):
    """Port for the mixed-source extraction agent."""

    async def execute(self, input_data: Any) -> AgentResult[MultiSourceExtractionOutput]:
        """Classify and extract a mixed source bundle."""
        ...


@runtime_checkable
class MultiSourceAggregationAgentPort(Protocol):
    """Port for the mixed-source synthesis agent."""

    async def execute(self, input_data: Any) -> AgentResult[MultiSourceAggregationOutput]:
        """Synthesize normalized bundle items into one provenance-aware output."""
        ...


@runtime_checkable
class RelationshipAnalysisAgentPort(Protocol):
    """Port for the relationship analysis agent."""

    async def execute(
        self, input_data: RelationshipAnalysisInput
    ) -> AgentResult[RelationshipAnalysisOutput]:
        """Analyze relationships between articles in a batch."""
        ...


__all__ = [
    "MultiSourceAggregationAgentPort",
    "MultiSourceExtractionAgentPort",
    "RelationshipAnalysisAgentPort",
]
