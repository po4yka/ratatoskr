"""Webhook subscription management endpoints."""

from __future__ import annotations

import time
from typing import Any, cast

import httpx
from fastapi import APIRouter, Depends, Query

from app.api.dependencies.database import get_webhook_repository
from app.api.exceptions import APIException, ErrorCode, ResourceNotFoundError
from app.api.models.requests import (  # noqa: TC001  # used at runtime in route body annotations
    CreateWebhookRequest,
    UpdateWebhookRequest,
)
from app.api.models.responses import (
    PaginationInfo,
    WebhookDeliveryResponse,
    WebhookSubscriptionResponse,
    success_response,
)
from app.api.routers.auth import get_current_user
from app.api.search_helpers import isotime
from app.core.logging_utils import get_logger
from app.domain.services.webhook_service import (
    build_webhook_payload,
    generate_webhook_secret,
    is_webhook_url_safe,
    sign_payload,
    validate_webhook_url,
)
from app.security.ssrf import make_safe_async_client

logger = get_logger(__name__)
router = APIRouter()

VALID_EVENT_TYPES = [
    "summary.created",
    "summary.updated",
    "request.completed",
    "request.failed",
    "tag.attached",
    "tag.detached",
    "collection.item_added",
]

MAX_SUBSCRIPTIONS_PER_USER = 10


def _mask_secret(secret: str) -> str:
    """Return only the last 8 characters of the secret, masking the rest."""
    if len(secret) <= 8:
        return secret
    return "*" * 8 + secret[-8:]


def _sub_to_response(sub: dict[str, Any]) -> WebhookSubscriptionResponse:
    """Convert a subscription dict to a response model with masked secret."""
    events = sub.get("events_json") or []
    if isinstance(events, str):
        import json

        events = json.loads(events)
    return WebhookSubscriptionResponse(
        id=sub["id"],
        name=sub.get("name"),
        url=sub["url"],
        events=events,
        enabled=sub["enabled"],
        status=sub["status"],
        secret_preview=_mask_secret(sub["secret"]),
        failure_count=sub.get("failure_count", 0),
        last_delivery_at=isotime(sub.get("last_delivery_at")) or None,
        created_at=isotime(sub["created_at"]),
        updated_at=isotime(sub["updated_at"]),
    )


def _delivery_to_response(d: dict[str, Any]) -> WebhookDeliveryResponse:
    """Convert a delivery dict to a response model."""
    return WebhookDeliveryResponse(
        id=d["id"],
        event_type=d["event_type"],
        response_status=d.get("response_status"),
        success=d["success"],
        attempt=d.get("attempt", 1),
        duration_ms=d.get("duration_ms"),
        error=d.get("error"),
        created_at=isotime(d["created_at"]),
    )


async def _verify_ownership(repo: Any, webhook_id: int, user_id: int) -> dict[str, Any]:
    """Verify the subscription exists and belongs to the user. Returns the sub dict."""
    sub = await repo.async_get_subscription_by_id(webhook_id)
    if sub is None or sub.get("is_deleted"):
        raise ResourceNotFoundError("WebhookSubscription", webhook_id)
    if sub["user"] != user_id:
        raise ResourceNotFoundError("WebhookSubscription", webhook_id)
    return cast("dict[str, Any]", sub)


def _validate_events(events: list[str]) -> None:
    """Raise if any event type is invalid."""
    invalid = [e for e in events if e not in VALID_EVENT_TYPES]
    if invalid:
        raise APIException(
            message=f"Invalid event types: {', '.join(invalid)}. "
            f"Valid types: {', '.join(VALID_EVENT_TYPES)}",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )


@router.get("/")
async def list_subscriptions(
    user: dict[str, Any] = Depends(get_current_user),
    webhook_repo: Any = Depends(get_webhook_repository),
) -> dict[str, Any]:
    """List user's webhook subscriptions."""
    subs = await webhook_repo.async_get_user_subscriptions(user["user_id"], enabled_only=False)
    items = [_sub_to_response(s) for s in subs]
    return success_response({"subscriptions": [i.model_dump(by_alias=True) for i in items]})


@router.post("/", status_code=201)
async def create_subscription(
    body: CreateWebhookRequest,
    user: dict[str, Any] = Depends(get_current_user),
    webhook_repo: Any = Depends(get_webhook_repository),
) -> dict[str, Any]:
    """Create a new webhook subscription."""
    # Validate URL
    url_valid, url_error = validate_webhook_url(body.url)
    if not url_valid:
        raise APIException(
            message=f"Invalid webhook URL: {url_error}",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )

    # Validate events
    _validate_events(body.events)

    # Enforce max subscriptions per user
    existing = await webhook_repo.async_get_user_subscriptions(user["user_id"], enabled_only=False)
    if len(existing) >= MAX_SUBSCRIPTIONS_PER_USER:
        raise APIException(
            message=f"Maximum of {MAX_SUBSCRIPTIONS_PER_USER} webhook subscriptions per user",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )

    secret = generate_webhook_secret()
    sub = await webhook_repo.async_create_subscription(
        user_id=user["user_id"],
        name=body.name,
        url=body.url,
        secret=secret,
        events=body.events,
    )

    response = _sub_to_response(sub)
    data = response.model_dump(by_alias=True)
    # Return full secret once on creation
    data["secret"] = secret
    return success_response(data)


@router.get("/{webhook_id}")
async def get_subscription(
    webhook_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    webhook_repo: Any = Depends(get_webhook_repository),
) -> dict[str, Any]:
    """Get a webhook subscription's details."""
    sub = await _verify_ownership(webhook_repo, webhook_id, user["user_id"])
    return success_response(_sub_to_response(sub))


@router.patch("/{webhook_id}")
async def update_subscription(
    webhook_id: int,
    body: UpdateWebhookRequest,
    user: dict[str, Any] = Depends(get_current_user),
    webhook_repo: Any = Depends(get_webhook_repository),
) -> dict[str, Any]:
    """Update a webhook subscription."""
    await _verify_ownership(webhook_repo, webhook_id, user["user_id"])

    update_fields: dict[str, Any] = {}
    if body.name is not None:
        update_fields["name"] = body.name
    if body.url is not None:
        url_valid, url_error = validate_webhook_url(body.url)
        if not url_valid:
            raise APIException(
                message=f"Invalid webhook URL: {url_error}",
                error_code=ErrorCode.VALIDATION_ERROR,
                status_code=400,
            )
        update_fields["url"] = body.url
    if body.events is not None:
        _validate_events(body.events)
        update_fields["events"] = body.events
    if body.enabled is not None:
        update_fields["enabled"] = body.enabled
        if body.enabled:
            update_fields["status"] = "active"

    if not update_fields:
        raise APIException(
            message="No fields to update",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )

    updated = await webhook_repo.async_update_subscription(
        webhook_id, user_id=user["user_id"], **update_fields
    )
    return success_response(_sub_to_response(updated))


@router.delete("/{webhook_id}")
async def delete_subscription(
    webhook_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    webhook_repo: Any = Depends(get_webhook_repository),
) -> dict[str, Any]:
    """Soft-delete a webhook subscription."""
    await _verify_ownership(webhook_repo, webhook_id, user["user_id"])
    await webhook_repo.async_delete_subscription(webhook_id, user_id=user["user_id"])
    return success_response({"deleted": True, "id": webhook_id})


@router.post("/{webhook_id}/test")
async def send_test_webhook(
    webhook_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    webhook_repo: Any = Depends(get_webhook_repository),
) -> dict[str, Any]:
    """Send a test event to the webhook URL and return the delivery result."""
    sub = await _verify_ownership(webhook_repo, webhook_id, user["user_id"])

    payload = build_webhook_payload(
        event_type="test",
        data={"message": "This is a test webhook delivery from Ratatoskr."},
    )

    import orjson

    payload_bytes = orjson.dumps(payload)
    signature = sign_payload(sub["secret"], payload_bytes)

    # Pre-delivery policy check catches stale/unsafe URLs before opening a
    # socket. The safe transport below re-resolves and pins the connection
    # target, closing the DNS-rebinding window between check and connect.
    url_safe, ssrf_error = is_webhook_url_safe(sub["url"])
    if not url_safe:
        raise APIException(
            message=f"Webhook URL failed SSRF safety check: {ssrf_error}",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )

    start_ms = time.monotonic_ns() // 1_000_000
    response_status: int | None = None
    response_body: str | None = None
    error_text: str | None = None
    delivery_success = False

    try:
        async with make_safe_async_client(timeout=10.0, follow_redirects=False) as client:
            resp = await client.post(
                sub["url"],
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Signature": signature,
                    "X-Webhook-Event": "test",
                    "User-Agent": "BiteSize-Webhook/1.0",
                },
            )
            response_status = resp.status_code
            response_body = resp.text[:2000]
            delivery_success = 200 <= resp.status_code < 300
    except httpx.HTTPError as exc:
        error_text = str(exc)[:500]

    duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms

    delivery = await webhook_repo.async_log_delivery(
        subscription_id=webhook_id,
        event_type="test",
        payload=payload,
        response_status=response_status,
        response_body=response_body,
        duration_ms=duration_ms,
        success=delivery_success,
        attempt=1,
        error=error_text,
    )

    return success_response(_delivery_to_response(delivery))


@router.get("/{webhook_id}/deliveries")
async def list_deliveries(
    webhook_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    webhook_repo: Any = Depends(get_webhook_repository),
) -> dict[str, Any]:
    """Return paginated delivery history for a webhook subscription."""
    await _verify_ownership(webhook_repo, webhook_id, user["user_id"])

    deliveries = await webhook_repo.async_get_deliveries(webhook_id, limit=limit, offset=offset)
    items = [_delivery_to_response(d) for d in deliveries]

    return success_response(
        {"deliveries": [i.model_dump(by_alias=True) for i in items]},
        pagination=PaginationInfo(
            total=len(items),
            limit=limit,
            offset=offset,
            has_more=len(items) == limit,
        ),
    )


@router.post("/{webhook_id}/rotate-secret")
async def rotate_secret(
    webhook_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    webhook_repo: Any = Depends(get_webhook_repository),
) -> dict[str, Any]:
    """Generate a new secret for the subscription. Returns the new secret once."""
    await _verify_ownership(webhook_repo, webhook_id, user["user_id"])

    new_secret = generate_webhook_secret()
    await webhook_repo.async_rotate_secret(webhook_id, new_secret, user_id=user["user_id"])

    return success_response({"id": webhook_id, "secret": new_secret})
