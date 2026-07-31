"""/rules must say that rules are not executed.

The engine was removed in dd46ff80 because it had never run once -- its only
trigger was an event no production code ever published. The command survived, so
a user can still create a rule and watch it sit at "0 runs" forever. Without the
note that reads as a rule that has not matched yet, which is a different and
fixable-sounding problem.

There was no coverage for this handler at all, which is how a caveat like this
gets quietly dropped in a later edit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.adapters.telegram.command_handlers.rules_handler import RulesHandler

# Asserted on meaning, not on the exact sentence: a reworded caveat is fine, a
# missing one is the regression. Importing the module constant instead would make
# these tests pass by construction.
_CAVEAT = "not executed"


def _handler(rules: list[dict[str, Any]], rule: dict[str, Any] | None = None) -> RulesHandler:
    repo = SimpleNamespace(
        async_get_user_rules=AsyncMock(return_value=rules),
        async_get_rule_by_id=AsyncMock(return_value=rule),
    )
    return RulesHandler(
        cfg=SimpleNamespace(),
        db=SimpleNamespace(),
        response_formatter=SimpleNamespace(),
        rule_repo_factory=lambda: repo,
    )


def _ctx(text: str = "/rules", uid: int = 42) -> tuple[Any, AsyncMock]:
    safe_reply = AsyncMock()
    ctx = SimpleNamespace(
        uid=uid,
        text=text,
        message=object(),
        response_formatter=SimpleNamespace(safe_reply=safe_reply),
    )
    return ctx, safe_reply


def _sent(safe_reply: AsyncMock) -> str:
    assert safe_reply.await_count == 1, "expected exactly one reply"
    return safe_reply.await_args.args[1]


class TestTheNoteIsAlwaysPresent:
    @pytest.mark.asyncio
    async def test_on_the_empty_list(self) -> None:
        """Said before the user creates one, not after they wonder why it never ran."""
        handler = _handler([])
        ctx, reply = _ctx()

        await handler._list_rules(ctx)

        assert _CAVEAT in _sent(reply)

    @pytest.mark.asyncio
    async def test_on_the_listing(self) -> None:
        handler = _handler([{"id": 1, "name": "Tag AI posts", "event_type": "summary_created"}])
        ctx, reply = _ctx()

        await handler._list_rules(ctx)

        text = _sent(reply)
        assert "0 runs" in text, "the run count is what the note explains"
        assert _CAVEAT in text

    @pytest.mark.asyncio
    async def test_on_the_detail_view(self) -> None:
        """The detail view is where a zero run count is most misleading."""
        handler = _handler(
            [],
            rule={
                "id": 7,
                "user": 42,
                "name": "Tag AI posts",
                "event_type": "summary_created",
                "enabled": True,
                "run_count": 0,
                "match_mode": "all",
                "conditions_json": [{"field": "title", "operator": "contains", "value": "AI"}],
                "actions_json": [{"type": "add_tag", "value": "ai"}],
                "last_triggered_at": None,
            },
        )
        ctx, reply = _ctx("/rules 7")

        await handler._show_rule_detail(ctx, "7")

        assert _CAVEAT in _sent(reply)


def test_all_three_replies_share_one_sentence() -> None:
    """One constant, so a reword cannot leave two replies disagreeing."""
    import inspect

    from app.adapters.telegram.command_handlers import rules_handler

    source = inspect.getsource(rules_handler)
    assert source.count("_NOT_EXECUTED_NOTE") >= 4, (
        "expected one definition plus a use in each of the three replies"
    )
    assert _CAVEAT in rules_handler._NOT_EXECUTED_NOTE


class TestTheNoteDoesNotDisplaceAnything:
    @pytest.mark.asyncio
    async def test_the_listing_still_carries_its_content(self) -> None:
        handler = _handler([{"id": 3, "name": "Archive old", "event_type": "summary_created"}])
        ctx, reply = _ctx()

        await handler._list_rules(ctx)

        text = _sent(reply)
        assert "Archive old" in text
        assert "/web/rules" in text

    @pytest.mark.asyncio
    async def test_the_detail_still_carries_conditions_and_actions(self) -> None:
        handler = _handler(
            [],
            rule={
                "id": 7,
                "user": 42,
                "name": "Tag AI posts",
                "event_type": "summary_created",
                "enabled": True,
                "run_count": 4,
                "match_mode": "any",
                "conditions_json": [{"field": "title", "operator": "contains", "value": "AI"}],
                "actions_json": [{"type": "add_tag", "value": "ai"}],
                "last_triggered_at": datetime.now(tz=UTC),
            },
        )
        ctx, reply = _ctx("/rules 7")

        await handler._show_rule_detail(ctx, "7")

        text = _sent(reply)
        assert "title contains" in text
        assert "add_tag" in text
        assert "any conditions" in text


class TestOwnership:
    @pytest.mark.asyncio
    async def test_another_users_rule_is_not_disclosed(self) -> None:
        """The user check is a defence-in-depth IDOR guard; keep it covered."""
        handler = _handler([], rule={"id": 7, "user": 999, "name": "Someone else's"})
        ctx, reply = _ctx("/rules 7", uid=42)

        await handler._show_rule_detail(ctx, "7")

        text = _sent(reply)
        assert "not found" in text
        assert "Someone else" not in text
