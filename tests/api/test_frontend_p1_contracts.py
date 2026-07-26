from unittest.mock import AsyncMock

import pytest

from app.api.models.requests import CreateRuleRequest, UpdateWebhookRequest
from app.api.routers import rules, webhooks


@pytest.mark.asyncio
async def test_rule_creation_preserves_initial_enabled_state() -> None:
    repo = AsyncMock()
    repo.async_get_user_rules.return_value = []
    repo.async_create_rule.return_value = {
        "id": 1,
        "name": "Disabled rule",
        "description": None,
        "enabled": False,
        "event_type": "summary.created",
        "match_mode": "all",
        "conditions_json": [],
        "actions_json": [{"type": "archive", "params": {}}],
        "priority": 0,
        "run_count": 0,
        "last_triggered_at": None,
        "created_at": None,
        "updated_at": None,
    }

    await rules.create_rule(
        body=CreateRuleRequest(
            name="Disabled rule",
            event_type="summary.created",
            actions=[{"type": "archive", "params": {}}],
            enabled=False,
        ),
        user={"user_id": 7},
        rule_repo=repo,
    )

    assert repo.async_create_rule.await_args.kwargs["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "enabled"),
    [("active", True), ("paused", False), ("disabled", False)],
)
async def test_webhook_status_updates_status_and_enabled(status: str, enabled: bool) -> None:
    repo = AsyncMock()
    subscription = {
        "id": 3,
        "user": 7,
        "name": "Hook",
        "url": "https://example.com/hook",
        "events_json": ["summary.created"],
        "enabled": enabled,
        "status": status,
        "secret": "secret-value",
        "failure_count": 0,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    repo.async_get_subscription_by_id.return_value = subscription
    repo.async_update_subscription.return_value = subscription

    await webhooks.update_subscription(
        webhook_id=3,
        body=UpdateWebhookRequest(status=status),
        user={"user_id": 7},
        webhook_repo=repo,
    )

    assert repo.async_update_subscription.await_args.kwargs["status"] == status
    assert repo.async_update_subscription.await_args.kwargs["enabled"] is enabled
