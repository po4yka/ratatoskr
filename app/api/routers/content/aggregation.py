"""Aggregation bundle endpoints."""

from __future__ import annotations

import contextlib
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Request

from app.api.dependencies.database import get_session_manager
from app.api.exceptions import (
    AuthorizationError,
    ProcessingError,
    ResourceNotFoundError,
    ValidationError,
)
from app.api.models.requests import CreateAggregationBundleRequest  # noqa: TC001
from app.api.models.responses import (
    AggregationCreateResponse,
    AggregationDetailResponse,
    AggregationListResponse,
    AggregationSourceBundle,
    AggregationSourceItem,
    success_response,
)
from app.api.models.responses.common import PaginationInfo
from app.api.routers.auth import get_current_user
from app.api.routers.auth.tokens import resolve_client_type
from app.application.dto.aggregation import SourceSubmission
from app.application.services.aggregation_rollout import AggregationRolloutGate
from app.application.services.multi_source_aggregation_service import (
    MultiSourceAggregationService,
)
from app.config import load_config
from app.core.logging_utils import generate_correlation_id
from app.di.api import resolve_api_runtime
from app.di.repositories import build_aggregation_session_repository, build_user_repository
from app.di.shared import build_async_audit_sink
from app.domain.models.source import AggregationSessionStatus
from app.domain.models.source import SourceKind
from app.observability.metrics import record_request
from app.security.ssrf import is_url_safe

router = APIRouter()


def _get_aggregation_workflow(request: Request) -> MultiSourceAggregationService:
    runtime = resolve_api_runtime(request)
    return MultiSourceAggregationService(
        content_extractor=runtime.background_processor.url_processor.content_extractor,
        aggregation_session_repo=build_aggregation_session_repository(runtime.db),
        llm_client=runtime.core.llm_client,
    )


def _get_rollout_gate(request: Request) -> AggregationRolloutGate:
    runtime = resolve_api_runtime(request)
    cfg = getattr(runtime, "cfg", None) or load_config(allow_stub_telegram=True)
    db = getattr(runtime, "db", None)
    return AggregationRolloutGate(
        cfg=cfg,
        user_repo=build_user_repository(db) if db is not None else None,
    )


def _resolve_db(request: Request) -> Any:
    with contextlib.suppress(RuntimeError):
        return resolve_api_runtime(request).db
    return get_session_manager(request)


def _ensure_public_bundle_urls(
    *,
    body: CreateAggregationBundleRequest,
    audit: Any,
    audit_context: dict[str, Any],
) -> None:
    for position, item in enumerate(body.items):
        url = str(item.url)
        safe, reason = is_url_safe(url)
        if safe:
            continue
        details = {
            **audit_context,
            "position": position,
            "url": url,
            "reason": reason,
        }
        audit("WARNING", "aggregation.bundle_create_blocked_ssrf", details)
        raise ValidationError(
            "Aggregation bundle contains a blocked URL",
            details={
                "position": position,
                "url": url,
                "reason": reason,
            },
        )


async def _ensure_aggregation_available(
    *,
    gate: AggregationRolloutGate,
    user_id: int,
) -> None:
    decision = await gate.evaluate(user_id)
    if decision.enabled:
        return
    if decision.stage.value == "disabled":
        raise ResourceNotFoundError("Aggregation feature", "v1/aggregations")
    raise AuthorizationError(decision.reason)


def _build_progress_payload(session: dict[str, Any]) -> dict[str, Any]:
    total_items = int(session.get("total_items") or 0)
    successful_count = int(session.get("successful_count") or 0)
    failed_count = int(session.get("failed_count") or 0)
    duplicate_count = int(session.get("duplicate_count") or 0)
    processed_items = min(total_items, successful_count + failed_count + duplicate_count)
    completion_percent = int(session.get("progress_percent") or 0)
    if total_items > 0 and completion_percent == 0 and processed_items > 0:
        completion_percent = int((processed_items / total_items) * 100)
    return {
        "totalItems": total_items,
        "processedItems": processed_items,
        "successfulCount": successful_count,
        "failedCount": failed_count,
        "duplicateCount": duplicate_count,
        "completionPercent": completion_percent,
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


def _safe_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text.startswith(("http://", "https://")):
        return None
    return text


def _metadata_value(*payloads: dict[str, Any], key: str) -> Any:
    for payload in payloads:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _document_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("normalized_document_json")
    return payload if isinstance(payload, dict) else {}


def _source_metadata(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("source_metadata_json")
    return payload if isinstance(payload, dict) else {}


def _extraction_metadata(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("extraction_metadata_json")
    return payload if isinstance(payload, dict) else {}


def _domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlparse(url).netloc or None
    except ValueError:
        return None


def _source_item_from_record(
    record: dict[str, Any],
    *,
    fallback_session_id: int | None = None,
) -> AggregationSourceItem:
    document = _document_payload(record)
    document_metadata = (
        document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    )
    extraction_metadata = _extraction_metadata(record)
    source_metadata = _source_metadata(record)
    original_url = _safe_url(record.get("original_value"))
    normalized_url = _safe_url(record.get("normalized_value")) or original_url
    title = (
        str(
            _metadata_value(
                document,
                extraction_metadata,
                source_metadata,
                record,
                key="title",
            )
            or record.get("title_hint")
            or ""
        ).strip()
        or None
    )
    author = _metadata_value(document_metadata, extraction_metadata, source_metadata, key="author")
    published_at = _metadata_value(
        document_metadata,
        extraction_metadata,
        source_metadata,
        key="published_at",
    )
    domain = _metadata_value(
        document_metadata, extraction_metadata, source_metadata, key="domain"
    ) or _domain_from_url(normalized_url)
    deleted = bool(record.get("request_is_deleted") or record.get("summary_is_deleted"))
    return AggregationSourceItem(
        bundle_id=int(record.get("aggregation_session_id") or fallback_session_id or 0),
        source_item_id=str(record.get("source_item_id") or ""),
        item_id=record.get("id"),
        position=int(record.get("position") or 0),
        original_url=original_url,
        normalized_url=normalized_url,
        source_kind=str(record.get("source_kind") or SourceKind.UNKNOWN.value),
        extraction_status=str(record.get("status") or "unknown"),
        title=title,
        domain=str(domain).strip() if domain else None,
        author=str(author).strip() if author else None,
        published_at=str(published_at).strip() if published_at else None,
        error_code=record.get("failure_code"),
        error_message=record.get("failure_message"),
        request_id=record.get("request_id"),
        crawl_result_id=record.get("crawl_result_id"),
        summary_id=None if deleted else record.get("summary_id"),
        duplicate_of_item_id=record.get("duplicate_of_item_id"),
        deleted=deleted,
        metadata={
            key: value
            for key, value in {
                "contentSource": document_metadata.get("content_source")
                or extraction_metadata.get("content_source"),
                "extractionSource": (document.get("provenance") or {}).get("extraction_source")
                if isinstance(document.get("provenance"), dict)
                else None,
            }.items()
            if value not in (None, "")
        },
    )


def _source_item_from_extraction_result(
    item: Any,
    *,
    session_id: int,
) -> AggregationSourceItem:
    document = item.normalized_document
    document_metadata = document.metadata if document is not None else {}
    original_url = _safe_url(document.provenance.original_value if document is not None else None)
    normalized_url = (
        _safe_url(document.provenance.normalized_value if document is not None else None)
        or original_url
    )
    failure = item.failure
    return AggregationSourceItem(
        bundle_id=session_id,
        source_item_id=item.source_item_id,
        item_id=item.item_id,
        position=item.position,
        original_url=original_url,
        normalized_url=normalized_url,
        source_kind=item.source_kind.value,
        extraction_status=item.status,
        title=document.title if document is not None else None,
        domain=str(
            document_metadata.get("domain") or _domain_from_url(normalized_url) or ""
        ).strip()
        or None,
        author=str(document_metadata.get("author") or "").strip() or None,
        published_at=str(document_metadata.get("published_at") or "").strip() or None,
        error_code=failure.code if failure is not None else None,
        error_message=failure.message if failure is not None else None,
        request_id=item.request_id,
        duplicate_of_item_id=item.duplicate_of_item_id,
        metadata={
            key: value
            for key, value in {
                "contentSource": document_metadata.get("content_source"),
                "extractionSource": document.provenance.extraction_source
                if document is not None
                else None,
            }.items()
            if value not in (None, "")
        },
    )


def _build_source_bundle(
    *,
    session_id: int,
    correlation_id: str | None,
    status: str | None,
    persisted_items: list[dict[str, Any]] | None = None,
    extraction_items: list[Any] | None = None,
) -> AggregationSourceBundle:
    if persisted_items is not None:
        items = [
            _source_item_from_record(item, fallback_session_id=session_id)
            for item in persisted_items
        ]
    else:
        items = [
            _source_item_from_extraction_result(item, session_id=session_id)
            for item in extraction_items or []
        ]
    return AggregationSourceBundle(
        bundle_id=session_id,
        correlation_id=correlation_id,
        status=status,
        items=items,
    )


def _serialize_persisted_session(session: dict[str, Any]) -> dict[str, Any]:
    return {
        **session,
        "progress": _build_progress_payload(session),
        "failure": _build_failure_payload(session),
    }


def _resolve_metric_source(user: dict[str, Any]) -> str:
    client_type = resolve_client_type(user.get("client_id"))
    return client_type if client_type != "unknown" else "api"


def _record_aggregation_api_metric(
    *,
    operation: str,
    user: dict[str, Any],
    status: str,
    started_at: float,
) -> None:
    record_request(
        request_type=f"aggregation.{operation}",
        status=status,
        source=_resolve_metric_source(user),
        latency_seconds=max(0.0, time.perf_counter() - started_at),
    )


@router.post("", response_model=AggregationCreateResponse)
async def create_aggregation_bundle(
    body: CreateAggregationBundleRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    workflow: MultiSourceAggregationService = Depends(_get_aggregation_workflow),
    rollout_gate: AggregationRolloutGate = Depends(_get_rollout_gate),
) -> dict[str, Any]:
    """Run mixed-source aggregation for one submitted bundle."""

    started_at = time.perf_counter()
    try:
        await _ensure_aggregation_available(gate=rollout_gate, user_id=user["user_id"])
        correlation_id = getattr(request.state, "correlation_id", None) or generate_correlation_id()
        runtime = resolve_api_runtime(request)
        audit = build_async_audit_sink(_resolve_db(request))
        audit_context = {
            "user_id": user["user_id"],
            "client_id": user.get("client_id"),
            "correlation_id": correlation_id,
            "item_count": len(body.items),
            "lang_preference": body.lang_preference,
        }
        audit("INFO", "aggregation.bundle_create_requested", audit_context)
        _ensure_public_bundle_urls(body=body, audit=audit, audit_context=audit_context)
        submissions = [
            SourceSubmission.from_url(
                str(item.url),
                metadata={
                    **dict(item.metadata or {}),
                    **(
                        {"source_kind_hint": item.source_kind_hint} if item.source_kind_hint else {}
                    ),
                },
            )
            for item in body.items
        ]
        repo = build_aggregation_session_repository(runtime.db)
        try:
            result = await workflow.aggregate(
                correlation_id=correlation_id,
                user_id=user["user_id"],
                submissions=submissions,
                language=body.lang_preference,
                metadata={
                    **dict(body.metadata or {}),
                    "entrypoint": "api",
                    "client_id": user.get("client_id"),
                },
            )
        except TimeoutError as exc:
            audit(
                "ERROR",
                "aggregation.bundle_create_failed",
                {
                    **audit_context,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "reason_code": "AGGREGATION_TIMEOUT",
                },
            )
            raise ProcessingError(
                "Aggregation request timed out",
                details={"reason_code": "AGGREGATION_TIMEOUT"},
            ) from exc
        except RuntimeError as exc:
            audit(
                "ERROR",
                "aggregation.bundle_create_failed",
                {
                    **audit_context,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "reason_code": "AGGREGATION_UPSTREAM_FAILURE",
                },
            )
            raise ProcessingError(
                "Aggregation request failed",
                details={
                    "reason_code": "AGGREGATION_UPSTREAM_FAILURE",
                    "upstream_error": str(exc),
                },
            ) from exc
        except Exception as exc:
            audit(
                "ERROR",
                "aggregation.bundle_create_failed",
                {
                    **audit_context,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise
        persisted_session = await repo.async_get_aggregation_session(result.aggregation.session_id)
        persisted_items = await repo.async_get_aggregation_session_items(
            result.aggregation.session_id
        )
        audit(
            "INFO",
            "aggregation.bundle_create_succeeded",
            {
                **audit_context,
                "session_id": result.aggregation.session_id,
                "status": result.aggregation.status,
                "successful_count": result.extraction.successful_count,
                "failed_count": result.extraction.failed_count,
                "duplicate_count": result.extraction.duplicate_count,
            },
        )
        progress_source = persisted_session or {
            "total_items": result.aggregation.total_items,
            "successful_count": result.extraction.successful_count,
            "failed_count": result.extraction.failed_count,
            "duplicate_count": result.extraction.duplicate_count,
            "progress_percent": 100 if result.aggregation.status != "failed" else 0,
        }
        source_bundle = _build_source_bundle(
            session_id=result.aggregation.session_id,
            correlation_id=result.aggregation.correlation_id,
            status=(persisted_session or {}).get("status", result.aggregation.status),
            persisted_items=persisted_items or None,
            extraction_items=result.extraction.items,
        )
        response = success_response(
            {
                "session": {
                    "sessionId": result.aggregation.session_id,
                    "correlationId": result.aggregation.correlation_id,
                    "status": (persisted_session or {}).get("status", result.aggregation.status),
                    "sourceType": result.aggregation.source_type,
                    "successfulCount": result.extraction.successful_count,
                    "failedCount": result.extraction.failed_count,
                    "duplicateCount": result.extraction.duplicate_count,
                    "processingTimeMs": (persisted_session or {}).get("processing_time_ms"),
                    "queuedAt": (persisted_session or {}).get("queued_at"),
                    "startedAt": (persisted_session or {}).get("started_at"),
                    "completedAt": (persisted_session or {}).get("completed_at"),
                    "lastProgressAt": (persisted_session or {}).get("last_progress_at"),
                    "progress": _build_progress_payload(progress_source),
                    "failure": _build_failure_payload(persisted_session or {}),
                },
                "aggregation": result.aggregation.model_dump(mode="json"),
                "items": [
                    {
                        "position": item.position,
                        "itemId": item.item_id,
                        "sourceItemId": item.source_item_id,
                        "sourceKind": item.source_kind.value,
                        "status": item.status,
                        "requestId": item.request_id,
                        "failure": item.failure.model_dump(mode="json") if item.failure else None,
                    }
                    for item in result.extraction.items
                ],
                "sourceBundle": source_bundle.model_dump(mode="json", by_alias=True),
            },
            correlation_id=correlation_id,
        )
        _record_aggregation_api_metric(
            operation="create",
            user=user,
            status="success",
            started_at=started_at,
        )
        return response
    except Exception:
        _record_aggregation_api_metric(
            operation="create",
            user=user,
            status="error",
            started_at=started_at,
        )
        raise


@router.get("", response_model=AggregationListResponse)
async def list_aggregation_bundles(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: AggregationSessionStatus | None = Query(default=None),
    user: dict[str, Any] = Depends(get_current_user),
    rollout_gate: AggregationRolloutGate = Depends(_get_rollout_gate),
) -> dict[str, Any]:
    """Return recent aggregation sessions for the authenticated user."""

    started_at = time.perf_counter()
    try:
        await _ensure_aggregation_available(gate=rollout_gate, user_id=user["user_id"])
        runtime = resolve_api_runtime(request)
        repo = build_aggregation_session_repository(runtime.db)
        status_value = status.value if status is not None else None
        sessions = await repo.async_get_user_aggregation_sessions(
            user["user_id"],
            limit=limit + 1,
            offset=offset,
            status=status_value,
        )
        total = await repo.async_count_user_aggregation_sessions(
            user["user_id"], status=status_value
        )
        has_more = len(sessions) > limit
        visible_sessions = sessions[:limit]
        response = success_response(
            {
                "sessions": [_serialize_persisted_session(session) for session in visible_sessions],
            },
            correlation_id=getattr(request.state, "correlation_id", None),
            pagination=PaginationInfo(
                total=total,
                limit=limit,
                offset=offset,
                has_more=has_more,
            ),
        )
        _record_aggregation_api_metric(
            operation="list",
            user=user,
            status="success",
            started_at=started_at,
        )
        return response
    except Exception:
        _record_aggregation_api_metric(
            operation="list",
            user=user,
            status="error",
            started_at=started_at,
        )
        raise


@router.get("/{session_id}", response_model=AggregationDetailResponse)
async def get_aggregation_bundle(
    session_id: int,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    rollout_gate: AggregationRolloutGate = Depends(_get_rollout_gate),
) -> dict[str, Any]:
    """Return one persisted aggregation session with bundle items and output."""

    started_at = time.perf_counter()
    try:
        await _ensure_aggregation_available(gate=rollout_gate, user_id=user["user_id"])
        runtime = resolve_api_runtime(request)
        repo = build_aggregation_session_repository(runtime.db)
        session = await repo.async_get_aggregation_session(session_id)
        if session is None:
            raise ResourceNotFoundError("Aggregation session", session_id)
        if session.get("user") != user["user_id"]:
            raise AuthorizationError("Access denied")

        items = await repo.async_get_aggregation_session_items(session_id)
        source_bundle = _build_source_bundle(
            session_id=session_id,
            correlation_id=session.get("correlation_id"),
            status=session.get("status"),
            persisted_items=items,
        )
        response = success_response(
            {
                "session": _serialize_persisted_session(session),
                "items": items,
                "aggregation": session.get("aggregation_output_json"),
                "sourceBundle": source_bundle.model_dump(mode="json", by_alias=True),
            },
            correlation_id=getattr(request.state, "correlation_id", None),
        )
        _record_aggregation_api_metric(
            operation="get",
            user=user,
            status="success",
            started_at=started_at,
        )
        return response
    except Exception:
        _record_aggregation_api_metric(
            operation="get",
            user=user,
            status="error",
            started_at=started_at,
        )
        raise
