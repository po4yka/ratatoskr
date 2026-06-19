"""Dispatch summary-created events to enabled export integrations."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select

from app.adapters.export.base import ExportPayload, ExportResult, payload_from_summary_context
from app.adapters.export.notion_export import NotionExportAdapter
from app.adapters.export.obsidian_export import ObsidianExportAdapter
from app.adapters.export.readwise_export import ReadwiseExportAdapter
from app.core.logging_utils import get_logger
from app.db.json_utils import prepare_json_payload
from app.db.models import ExportDeliveryLog, UserExportIntegration
from app.security.token_crypto import decrypt_token

logger = get_logger(__name__)
SUPPORTED_EXPORT_PROVIDERS = frozenset({"notion", "readwise", "obsidian"})


class SummaryExportDispatcher:
    """Best-effort publisher for summary.created export events."""

    def __init__(self, database: Any) -> None:
        self._database = database

    async def publish_summary_created(self, summary_id: int) -> None:
        async with self._database.session() as session:
            summary_repo_context = await _load_summary_context(session, summary_id)
            if summary_repo_context is None:
                return
            summary = summary_repo_context["summary"]
            user_id = summary.get("user_id")
            if not isinstance(user_id, int):
                return
            integrations = (
                (
                    await session.execute(
                        select(UserExportIntegration).where(
                            UserExportIntegration.user_id == user_id,
                            UserExportIntegration.enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )

        if not integrations:
            return
        payload = payload_from_summary_context(summary_repo_context)
        for integration in integrations:
            await self._deliver(integration, payload)

    async def publish_summary_created_to_integration(
        self, *, summary_id: int, integration_id: int, user_id: int
    ) -> bool:
        async with self._database.session() as session:
            summary_repo_context = await _load_summary_context(session, summary_id)
            if summary_repo_context is None:
                return False
            summary = summary_repo_context["summary"]
            if summary.get("user_id") != user_id:
                return False
            integration = await session.get(UserExportIntegration, integration_id)
            if integration is None or integration.user_id != user_id:
                return False
        await self._deliver(integration, payload_from_summary_context(summary_repo_context))
        return True

    async def _deliver(self, integration: UserExportIntegration, payload: ExportPayload) -> None:
        started = time.perf_counter()
        try:
            adapter = _adapter_for_integration(integration)
            result = await adapter.export(payload)
        except Exception as exc:
            logger.warning(
                "export_connector_delivery_failed",
                extra={
                    "integration_id": integration.id,
                    "provider": integration.provider,
                    "summary_id": payload.summary_id,
                    "error": str(exc),
                },
                exc_info=True,
            )
            result = ExportResult(success=False, error=str(exc))
        duration_ms = int((time.perf_counter() - started) * 1000)
        await _log_delivery(
            self._database,
            integration=integration,
            payload=payload,
            result=result,
            duration_ms=duration_ms,
        )


async def _load_summary_context(session: Any, summary_id: int) -> dict[str, Any] | None:
    from app.db.models import CrawlResult, Request, Summary, TranscriptionArtifact, model_to_dict

    row = (
        await session.execute(
            select(Summary, Request, CrawlResult, TranscriptionArtifact)
            .join(Request, Summary.request_id == Request.id)
            .outerjoin(CrawlResult, CrawlResult.request_id == Request.id)
            .outerjoin(TranscriptionArtifact, TranscriptionArtifact.request_id == Request.id)
            .where(Summary.id == summary_id)
            .order_by(TranscriptionArtifact.created_at.desc().nullslast())
        )
    ).first()
    if row is None:
        return None
    summary, request, crawl_result, transcription_artifact = row
    summary_data = model_to_dict(summary) or {}
    summary_data["user_id"] = request.user_id
    return {
        "summary": summary_data,
        "request": model_to_dict(request),
        "crawl_result": model_to_dict(crawl_result),
        "transcription_artifact": model_to_dict(transcription_artifact),
    }


def _adapter_for_integration(integration: UserExportIntegration) -> Any:
    provider = integration.provider
    config = dict(integration.config_json) if isinstance(integration.config_json, dict) else {}
    token = decrypt_token(integration.encrypted_token) if integration.encrypted_token else ""
    if provider == "notion":
        return NotionExportAdapter(token=token, database_id=str(config.get("database_id") or ""))
    if provider == "readwise":
        return ReadwiseExportAdapter(token=token)
    if provider == "obsidian":
        return ObsidianExportAdapter(
            vault_path=str(config.get("vault_path") or ""),
            folder=str(config.get("folder") or "") or None,
        )
    msg = f"Unsupported export provider: {provider}"
    raise ValueError(msg)


async def _log_delivery(
    database: Any,
    *,
    integration: UserExportIntegration,
    payload: ExportPayload,
    result: ExportResult,
    duration_ms: int,
) -> None:
    async with database.transaction() as session:
        session.add(
            ExportDeliveryLog(
                integration_id=integration.id,
                provider=integration.provider,
                event_type="summary.created",
                summary_id=payload.summary_id,
                payload_json=prepare_json_payload(
                    {
                        "summary_id": payload.summary_id,
                        "request_id": payload.request_id,
                        "title": payload.title,
                        "url": payload.url,
                    },
                    default={},
                ),
                response_status=result.response_status,
                response_body=result.response_body,
                duration_ms=duration_ms,
                success=result.success,
                attempt=1,
                error=result.error,
            )
        )
