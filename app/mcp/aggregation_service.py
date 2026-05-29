from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from app.adapters.content.multi_source_classification import classify_url_source_kind
from app.agents.multi_source_aggregation_agent import MultiSourceAggregationAgent
from app.agents.multi_source_extraction_agent import MultiSourceExtractionAgent
from app.agents.relationship_analysis_agent import RelationshipAnalysisAgent
from app.api.models.requests import AggregationBundleItemRequest, CreateAggregationBundleRequest
from app.application.dto.aggregation import SourceSubmission
from app.application.services.aggregation_rollout import AggregationRolloutGate
from app.application.services.multi_source_aggregation_service import (
    MultiSourceAggregationRunResult,
    MultiSourceAggregationService,
)
from app.core.logging_utils import generate_correlation_id
from app.core.url_utils import normalize_url
from app.di.repositories import build_aggregation_session_repository, build_user_repository
from app.domain.models.source import AggregationSessionStatus

logger = logging.getLogger("ratatoskr.mcp")

if TYPE_CHECKING:
    from app.mcp.context import McpServerContext


def _build_progress_payload(session: dict[str, Any]) -> dict[str, int]:
    total_items = int(session.get("total_items") or 0)
    successful_count = int(session.get("successful_count") or 0)
    failed_count = int(session.get("failed_count") or 0)
    duplicate_count = int(session.get("duplicate_count") or 0)
    processed_items = min(total_items, successful_count + failed_count + duplicate_count)
    completion_percent = int(session.get("progress_percent") or 0)
    if total_items > 0 and completion_percent == 0 and processed_items > 0:
        completion_percent = int((processed_items / total_items) * 100)
    return {
        "total_items": total_items,
        "processed_items": processed_items,
        "successful_count": successful_count,
        "failed_count": failed_count,
        "duplicate_count": duplicate_count,
        "completion_percent": completion_percent,
    }


def _build_failure_payload(record: dict[str, Any]) -> dict[str, Any] | None:
    code = str(record.get("failure_code") or "").strip()
    message = str(record.get("failure_message") or "").strip()
    details = record.get("failure_details_json")
    if not code and not message and not details:
        return None
    return {
        "code": code or None,
        "message": message or None,
        "details": details,
    }


class AggregationMcpService:
    """Aggregation-specific MCP operations for local and trusted scoped use."""

    def __init__(self, context: McpServerContext) -> None:
        self.context = context

    def _require_scoped_user(self) -> int | dict[str, Any]:
        if self.context.user_id is None:
            return {
                "error": (
                    "Aggregation MCP tools require a scoped user. "
                    "Set MCP_USER_ID / --user-id for local mode or authenticate the HTTP request."
                )
            }
        return int(self.context.user_id)

    def _serialize_session(self, session: dict[str, Any]) -> dict[str, Any]:
        payload = dict(session)
        aggregation = payload.get("aggregation_output_json")
        if isinstance(aggregation, dict):
            if "source_type" in aggregation:
                payload.setdefault("source_type", aggregation.get("source_type"))
            if "overview" in aggregation:
                payload.setdefault("overview", aggregation.get("overview"))
        payload["progress"] = _build_progress_payload(payload)
        payload["failure"] = _build_failure_payload(payload)
        return payload

    async def _ensure_enabled(self, runtime: Any, user_id: int) -> dict[str, Any] | None:
        gate = AggregationRolloutGate(
            cfg=runtime.cfg,
            user_repo=build_user_repository(runtime.db),
        )
        decision = await gate.evaluate(user_id)
        if decision.enabled:
            return None
        return {
            "error": decision.reason,
            "rollout_stage": decision.stage.value,
        }

    async def create_aggregation_bundle(
        self,
        items: list[dict[str, Any]],
        lang_preference: str = "auto",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create and execute a mixed-source aggregation bundle for the scoped user."""
        scoped_user = self._require_scoped_user()
        if isinstance(scoped_user, dict):
            return scoped_user

        try:
            payload = CreateAggregationBundleRequest.model_validate(
                {
                    "items": items,
                    "lang_preference": lang_preference,
                    "metadata": metadata,
                }
            )
            runtime = await self.context.ensure_api_runtime()
            rollout_error = await self._ensure_enabled(runtime, scoped_user)
            if rollout_error is not None:
                return rollout_error

            repo = build_aggregation_session_repository(runtime.db)
            workflow = MultiSourceAggregationService(
                extraction_agent=MultiSourceExtractionAgent(
                    content_extractor=runtime.background_processor.url_processor.content_extractor,
                    aggregation_session_repo=repo,
                ),
                aggregation_agent=MultiSourceAggregationAgent(
                    aggregation_session_repo=repo,
                    llm_client=runtime.core.llm_client,
                ),
                aggregation_session_repo=repo,
                relationship_agent=RelationshipAnalysisAgent(
                    llm_client=runtime.core.llm_client,
                )
                if runtime.core.llm_client is not None
                else None,
            )
            submissions = [
                SourceSubmission.from_url(
                    str(item.url),
                    metadata={
                        **dict(item.metadata or {}),
                        **(
                            {"source_kind_hint": item.source_kind_hint}
                            if item.source_kind_hint
                            else {}
                        ),
                    },
                )
                for item in payload.items
            ]
            result = await workflow.aggregate(
                correlation_id=generate_correlation_id(),
                user_id=scoped_user,
                submissions=submissions,
                language=payload.lang_preference,
                metadata={
                    **dict(payload.metadata or {}),
                    "entrypoint": "mcp",
                    "client_id": self.context.client_id,
                },
            )
            persisted_session = await repo.async_get_aggregation_session(
                result.aggregation.session_id
            )
            return self._serialize_create_result(result, persisted_session)
        except ValidationError as exc:
            return {"error": str(exc), "details": exc.errors()}
        except Exception as exc:
            logger.exception("mcp_create_aggregation_bundle_failed")
            return {"error": str(exc)}

    def _serialize_create_result(
        self,
        result: MultiSourceAggregationRunResult,
        persisted_session: dict[str, Any] | None,
    ) -> dict[str, Any]:
        session_payload = dict(persisted_session or {})
        session_payload.setdefault("id", result.aggregation.session_id)
        session_payload.setdefault("correlation_id", result.aggregation.correlation_id)
        session_payload.setdefault("status", result.aggregation.status)
        session_payload.setdefault("total_items", result.aggregation.total_items)
        session_payload.setdefault("successful_count", result.extraction.successful_count)
        session_payload.setdefault("failed_count", result.extraction.failed_count)
        session_payload.setdefault("duplicate_count", result.extraction.duplicate_count)
        session_payload.setdefault("source_type", result.aggregation.source_type)
        session_payload["source_type"] = result.aggregation.source_type
        session_payload["successful_count"] = result.extraction.successful_count
        session_payload["failed_count"] = result.extraction.failed_count
        session_payload["duplicate_count"] = result.extraction.duplicate_count
        return {
            "session": self._serialize_session(session_payload),
            "aggregation": result.aggregation.model_dump(mode="json"),
            "items": [
                {
                    "position": item.position,
                    "item_id": item.item_id,
                    "source_item_id": item.source_item_id,
                    "source_kind": item.source_kind.value,
                    "status": item.status,
                    "request_id": item.request_id,
                    "failure": item.failure.model_dump(mode="json") if item.failure else None,
                }
                for item in result.extraction.items
            ],
        }

    async def get_aggregation_bundle(self, session_id: int) -> dict[str, Any]:
        """Return one persisted aggregation bundle for the scoped user."""
        scoped_user = self._require_scoped_user()
        if isinstance(scoped_user, dict):
            return scoped_user

        try:
            runtime = await self.context.ensure_api_runtime()
            rollout_error = await self._ensure_enabled(runtime, scoped_user)
            if rollout_error is not None:
                return rollout_error

            repo = build_aggregation_session_repository(runtime.db)
            session = await repo.async_get_aggregation_session(session_id)
            if session is None:
                return {"error": f"Aggregation session {session_id} not found"}
            if int(session.get("user") or 0) != scoped_user:
                return {"error": "Access denied"}
            items = await repo.async_get_aggregation_session_items(session_id)
            return {
                "session": self._serialize_session(session),
                "items": items,
                "aggregation": session.get("aggregation_output_json"),
            }
        except Exception as exc:
            logger.exception("mcp_get_aggregation_bundle_failed")
            return {"error": str(exc)}

    async def list_aggregation_bundles(
        self,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
    ) -> dict[str, Any]:
        """List persisted aggregation bundles for the scoped user."""
        scoped_user = self._require_scoped_user()
        if isinstance(scoped_user, dict):
            return scoped_user

        try:
            limit = max(1, min(100, int(limit)))
            offset = max(0, int(offset))
            status_value: str | None = None
            if status:
                status_value = AggregationSessionStatus(status).value

            runtime = await self.context.ensure_api_runtime()
            rollout_error = await self._ensure_enabled(runtime, scoped_user)
            if rollout_error is not None:
                return rollout_error

            repo = build_aggregation_session_repository(runtime.db)
            sessions = await repo.async_get_user_aggregation_sessions(
                scoped_user,
                limit=limit + 1,
                offset=offset,
                status=status_value,
            )
            total = await repo.async_count_user_aggregation_sessions(
                scoped_user,
                status=status_value,
            )
            has_more = len(sessions) > limit
            visible_sessions = sessions[:limit]
            return {
                "sessions": [self._serialize_session(session) for session in visible_sessions],
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": has_more,
            }
        except ValueError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception("mcp_list_aggregation_bundles_failed")
            return {"error": str(exc)}

    def check_source_supported(
        self,
        url: str,
        source_kind_hint: str | None = None,
    ) -> dict[str, Any]:
        """Classify a URL into the public aggregation source contract."""
        try:
            item = AggregationBundleItemRequest.model_validate(
                {
                    "url": url,
                    "source_kind_hint": source_kind_hint,
                }
            )
            normalized_url = normalize_url(str(item.url))
            source_kind = classify_url_source_kind(
                normalized_url,
                hint=item.source_kind_hint,
            )
            return {
                "supported": True,
                "url": str(item.url),
                "normalized_url": normalized_url,
                "source_kind": source_kind.value,
                "source_kind_hint": item.source_kind_hint,
            }
        except ValidationError as exc:
            return {
                "supported": False,
                "error": str(exc),
                "details": exc.errors(),
            }
        except Exception as exc:
            logger.exception("mcp_check_source_supported_failed")
            return {"supported": False, "error": str(exc)}
