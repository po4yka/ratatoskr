"""Signal feed endpoints for Phase 3 triage."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.dependencies.signals import get_signal_source_repository
from app.api.models.responses import success_response
from app.api.models.signals import (
    SignalFeedbackRequest,
    SignalHealthSuccessResponse,
    SignalListSuccessResponse,
    SignalQueuedSuccessResponse,
    SignalSourcesHealthSuccessResponse,
    SignalTopicSuccessResponse,
    SignalUpdatedSuccessResponse,
    SourceActiveRequest,
    SourceControlRequest,
    TopicPreferenceRequest,
)
from app.api.routers.auth import get_current_user
from app.application.ports.signal_sources import (  # noqa: TC001  # used at runtime in route signatures
    SignalSourceRepositoryPort,
)
from app.application.services.signal_personalization import SignalPersonalizationService
from app.di.api import resolve_api_runtime

router = APIRouter()


def _user_id(user: dict[str, Any]) -> int:
    return int(user["user_id"])


@router.get("", response_model=SignalListSuccessResponse, response_model_exclude_none=True)
async def list_signals(
    repo: SignalSourceRepositoryPort = Depends(get_signal_source_repository),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    signals = await repo.async_list_user_signals(_user_id(user), limit=50)
    return success_response({"signals": signals})


@router.get("/health", response_model=SignalHealthSuccessResponse, response_model_exclude_none=True)
async def signal_health(
    request: Request,
    repo: SignalSourceRepositoryPort = Depends(get_signal_source_repository),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    sources = await repo.async_list_source_health(user_id=_user_id(user))
    vector = {"ready": False, "required": True, "collection": None}
    try:
        runtime = resolve_api_runtime(request)
        vector_store = getattr(runtime.search, "vector_store", None)
        ready = False
        if vector_store is not None:
            health_check = getattr(vector_store, "health_check", None)
            ready = (
                bool(health_check())
                if callable(health_check)
                else bool(getattr(vector_store, "available", False))
            )
        vector = {
            "ready": ready,
            "required": bool(getattr(runtime.cfg.vector_store, "required", True)),
            "collection": getattr(vector_store, "collection_name", None),
        }
    except RuntimeError:
        pass
    return success_response(
        {
            "vector": vector,
            "sources": {
                "total": len(sources),
                "active": sum(1 for row in sources if row.get("is_active")),
                "errored": sum(1 for row in sources if int(row.get("fetch_error_count") or 0) > 0),
            },
        }
    )


@router.get(
    "/sources/health",
    response_model=SignalSourcesHealthSuccessResponse,
    response_model_exclude_none=True,
)
async def source_health(
    repo: SignalSourceRepositoryPort = Depends(get_signal_source_repository),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    rows = await repo.async_list_source_health(user_id=_user_id(user))
    return success_response({"sources": rows})


@router.post(
    "/sources/{source_id}/active",
    response_model=SignalUpdatedSuccessResponse,
    response_model_exclude_none=True,
)
async def set_source_active(
    source_id: int,
    body: SourceActiveRequest,
    repo: SignalSourceRepositoryPort = Depends(get_signal_source_repository),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    updated = await repo.async_set_user_source_active(
        user_id=_user_id(user),
        source_id=source_id,
        is_active=body.is_active,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Source not found")
    return success_response({"updated": True, "is_active": body.is_active})


@router.patch(
    "/sources/{source_id}/controls",
    response_model=SignalUpdatedSuccessResponse,
    response_model_exclude_none=True,
)
async def update_source_controls(
    source_id: int,
    body: SourceControlRequest,
    repo: SignalSourceRepositoryPort = Depends(get_signal_source_repository),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    updated = await repo.async_update_user_source_controls(
        user_id=_user_id(user),
        source_id=source_id,
        is_active=body.is_active,
        fetch_interval_seconds=body.fetch_interval_seconds,
        max_items_per_run=body.max_items_per_run,
        retry_policy=body.retry_policy,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Source not found")
    return success_response({"updated": True})


@router.post(
    "/sources/{source_id}/retry",
    response_model=SignalQueuedSuccessResponse,
    response_model_exclude_none=True,
)
async def retry_source(
    source_id: int,
    repo: SignalSourceRepositoryPort = Depends(get_signal_source_repository),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    updated = await repo.async_retry_user_source(user_id=_user_id(user), source_id=source_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Source not found")
    return success_response({"queued": True})


@router.post(
    "/{signal_id}/feedback",
    response_model=SignalUpdatedSuccessResponse,
    response_model_exclude_none=True,
)
async def update_signal_feedback(
    signal_id: int,
    body: SignalFeedbackRequest,
    request: Request,
    repo: SignalSourceRepositoryPort = Depends(get_signal_source_repository),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if body.action == "hide_source":
        updated = await repo.async_hide_signal_source(user_id=_user_id(user), signal_id=signal_id)
    elif body.action == "boost_topic":
        updated = await repo.async_boost_signal_topic(user_id=_user_id(user), signal_id=signal_id)
    else:
        status = {
            "like": "liked",
            "dislike": "dismissed",
            "skip": "skipped",
            "queue": "queued",
        }[body.action]
        updated = await repo.async_update_user_signal_status(
            user_id=_user_id(user),
            signal_id=signal_id,
            status=status,
        )
        if updated and body.action == "like":
            await _embed_liked_signal(
                request=request, repo=repo, user_id=_user_id(user), signal_id=signal_id
            )
    if not updated:
        raise HTTPException(status_code=404, detail="Signal not found")
    return success_response({"updated": True})


@router.post("/topics", response_model=SignalTopicSuccessResponse, response_model_exclude_none=True)
async def upsert_topic(
    body: TopicPreferenceRequest,
    request: Request,
    repo: SignalSourceRepositoryPort = Depends(get_signal_source_repository),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    user_id = _user_id(user)
    topic = await repo.async_upsert_topic(
        user_id=user_id,
        name=body.name,
        description=body.description,
        weight=body.weight,
    )
    try:
        runtime = resolve_api_runtime(request)
        vector_store = getattr(runtime.search, "vector_store", None)
        embedding_service = getattr(runtime.search, "embedding_service", None)
        if vector_store is not None and embedding_service is not None:
            embedding_ref = await SignalPersonalizationService(
                vector_store=vector_store,
                embedding_service=embedding_service,
            ).embed_topic(
                user_id=user_id,
                topic_id=int(topic["id"]),
                name=body.name,
                description=body.description,
                weight=body.weight,
            )
            if embedding_ref is not None:
                topic = await repo.async_upsert_topic(
                    user_id=user_id,
                    name=body.name,
                    description=body.description,
                    weight=body.weight,
                    embedding_ref=embedding_ref,
                    metadata={"embedding_ref": embedding_ref},
                )
    except RuntimeError:
        pass
    return success_response({"topic": topic})


async def _embed_liked_signal(
    *,
    request: Request,
    repo: SignalSourceRepositoryPort,
    user_id: int,
    signal_id: int,
) -> None:
    try:
        runtime = resolve_api_runtime(request)
        vector_store = getattr(runtime.search, "vector_store", None)
        embedding_service = getattr(runtime.search, "embedding_service", None)
        if vector_store is None or embedding_service is None:
            return
        detail = await repo.async_get_user_signal(user_id=user_id, signal_id=signal_id)
        if detail is None:
            return
        await SignalPersonalizationService(
            vector_store=vector_store,
            embedding_service=embedding_service,
        ).embed_liked_feed_item(
            user_id=user_id,
            feed_item_id=int(detail["feed_item_id"]),
            title=detail.get("feed_item_title"),
            content_text=detail.get("feed_item_content_text"),
            canonical_url=detail.get("feed_item_url"),
        )
    except RuntimeError:
        return
