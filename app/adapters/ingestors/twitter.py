"""Cost-gated X/Twitter source ingester placeholder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.application.ports.source_ingestors import IngestedSource
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.application.ports.source_ingestors import SourceFetchResult

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class TwitterIngestionConfig:
    enabled: bool = False
    ack_cost: bool = False
    queries: tuple[str, ...] = field(default_factory=tuple)


class TwitterIngester:
    """Guard X/Twitter ingestion behind explicit opt-in and cost acknowledgment."""

    name = "twitter"

    def __init__(self, config: TwitterIngestionConfig | None = None) -> None:
        self.config = config or TwitterIngestionConfig()
        if self.config.enabled and not self.config.ack_cost:
            logger.warning(
                "twitter_ingestion_disabled_cost_ack_missing",
                extra={"required": "TWITTER_INGESTION_ACK_COST=true"},
            )

    def is_enabled(self) -> bool:
        return self.config.enabled and self.config.ack_cost

    def source_identity(self) -> IngestedSource:
        return IngestedSource(
            kind="twitter",
            external_id="twitter:configured",
            title="X/Twitter configured sources",
            metadata={"queries": list(self.config.queries)},
        )

    async def fetch(self) -> SourceFetchResult:
        if not self.config.ack_cost:
            raise RuntimeError("Twitter ingestion requires explicit cost acknowledgement")
        raise NotImplementedError("Twitter/X ingestion needs a BYO-token adapter before polling")
