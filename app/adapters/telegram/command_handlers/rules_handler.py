"""Automation rule listing command handler (/rules).

Read-only listing of user automation rules via Telegram.
Rules are managed via the web UI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.adapters.telegram.command_handlers.base_handler import HandlerDependenciesMixin
from app.adapters.telegram.command_handlers.decorators import combined_handler
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )

logger = get_logger(__name__)


class RulesHandler(HandlerDependenciesMixin):
    """Handle /rules command -- list automation rules (read-only)."""

    def __init__(
        self,
        cfg: Any,
        db: Any,
        response_formatter: Any,
        *,
        rule_repo_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(cfg, db, response_formatter)
        self._rule_repo_factory = rule_repo_factory

    @property
    def _rule_repo(self) -> Any:
        if self._rule_repo_factory is None:
            msg = "Rule repository factory is not configured"
            raise RuntimeError(msg)
        return self._rule_repo_factory()

    @combined_handler("command_rules", "rules", include_text=True)
    async def handle_rules(self, ctx: CommandExecutionContext) -> None:
        """Handle /rules [id].

        No arguments: list all enabled rules with run counts.
        With argument: show detailed rule info.
        """
        rule_arg = _parse_rules_arg(ctx.text)

        if rule_arg is not None:
            await self._show_rule_detail(ctx, rule_arg)
        else:
            await self._list_rules(ctx)

    async def _list_rules(self, ctx: CommandExecutionContext) -> None:
        """List all enabled rules for the user."""
        rules = await self._rule_repo.async_get_user_rules(ctx.uid, enabled_only=True)

        lines: list[str] = []
        for rule in rules:
            event = rule.get("event_type") or "unknown"
            runs = rule.get("run_count") or 0
            lines.append(f"{rule.get('id')}. {rule.get('name')} ({event}) - {runs} runs")

        if not lines:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "No automation rules yet. Create rules at: /web/rules",
            )
            return

        text = "Your automation rules:\n\n" + "\n".join(lines) + "\n\nManage rules at: /web/rules"
        await ctx.response_formatter.safe_reply(ctx.message, text)

    async def _show_rule_detail(self, ctx: CommandExecutionContext, rule_id_str: str) -> None:
        """Show detailed info for a specific rule."""
        try:
            rule_id = int(rule_id_str)
        except ValueError:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "Usage: /rules <id> (numeric rule ID)",
            )
            return

        rule = await self._rule_repo.async_get_rule_by_id(rule_id)

        if rule is None or int(rule.get("user") or 0) != ctx.uid or bool(rule.get("is_deleted")):
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"Rule #{rule_id} not found.",
            )
            return

        # Build detail text
        status = "enabled" if rule.get("enabled") else "disabled"
        runs = rule.get("run_count") or 0
        match_mode = rule.get("match_mode") or "all"

        parts: list[str] = [
            f"Rule #{rule.get('id')}: {rule.get('name')}",
            f"Event: {rule.get('event_type')}",
            f"Match: {match_mode} conditions",
        ]

        # Conditions
        conditions = rule.get("conditions_json") or []
        if conditions:
            parts.append("Conditions:")
            for cond in conditions:
                field = cond.get("field", "?")
                op = cond.get("operator", "?")
                value = cond.get("value", "?")
                parts.append(f"  - {field} {op} {_quote_value(value)}")

        # Actions
        actions = rule.get("actions_json") or []
        if actions:
            parts.append("Actions:")
            for action in actions:
                action_type = action.get("type", "?")
                action_value = action.get("value", "")
                if action_value:
                    parts.append(f"  - {action_type}: {_quote_value(action_value)}")
                else:
                    parts.append(f"  - {action_type}")

        # Status line
        last_triggered = _format_relative_time(rule.get("last_triggered_at"))
        status_parts = [status, f"{runs} runs"]
        if last_triggered:
            status_parts.append(f"Last: {last_triggered}")
        parts.append("Status: " + " | ".join(status_parts))

        parts.append("\nManage at: /web/rules")

        text = "\n".join(parts)
        await ctx.response_formatter.safe_reply(ctx.message, text)


def _parse_rules_arg(text: str) -> str | None:
    """Extract the argument after /rules, e.g. '/rules 5' -> '5'."""
    rest = text[len("/rules") :].strip()
    return rest if rest else None


def _quote_value(value: object) -> str:
    """Format a condition/action value for display."""
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def _format_relative_time(dt: datetime | None) -> str | None:
    """Format a datetime as a human-readable relative time string."""
    if dt is None:
        return None

    now = datetime.now(tz=UTC)
    # Ensure dt is timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    delta = now - dt
    seconds = int(delta.total_seconds())

    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}m ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h ago"
    days = seconds // 86400
    return f"{days}d ago"
