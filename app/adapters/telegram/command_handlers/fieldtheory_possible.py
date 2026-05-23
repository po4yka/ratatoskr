"""`/fieldtheory_possible` handler: surface the newest Possible-run idea nodes.

The host runs `ft possible run --defaults --background` on its own cadence;
this handler is **read-only** against the bind-mounted ``ideas/`` directory
(see ``docs/explanation/fieldtheory-integration.md`` Telegram Surface section).
It picks the newest ``*.json`` file by mtime, parses its node list, and
formats the top-N entries as a Telegram reply.

No subprocess call to ``ft`` is made; the container performs zero ``ft``
invocations per DEC-002 (host-side ft + read-only mount).
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import TYPE_CHECKING, Any

from app.adapters.telegram.command_handlers.decorators import combined_handler
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )
    from app.config import AppConfig

logger = get_logger(__name__)

_TOP_N_DEFAULT = 5
_NODE_TITLE_KEYS = ("title", "name", "goal", "headline", "summary")
_NODE_BODY_KEYS = ("prompt", "description", "rationale", "body", "text")
_NODES_CONTAINER_KEYS = ("nodes", "ideas", "items", "results")


class FieldTheoryPossibleHandler:
    """Read the newest ft Possible-run output and reply with its top nodes.

    Owner-gating is inherited from ``AccessController.check_access`` which
    runs upstream in the message router; this handler does not re-check.
    """

    def __init__(self, cfg: AppConfig, top_n: int = _TOP_N_DEFAULT) -> None:
        self._cfg = cfg
        self._top_n = top_n

    @combined_handler("command_fieldtheory_possible", "fieldtheory_possible")
    async def handle_fieldtheory_possible(self, ctx: CommandExecutionContext) -> None:
        """List newest ideas/*.json, format top-N node summaries as a reply."""
        ideas_path = pathlib.Path(self._cfg.fieldtheory.ideas_path)
        correlation_id = getattr(ctx, "correlation_id", None) or "unknown"

        newest = await asyncio.to_thread(_find_newest_ideas_file, ideas_path)
        if newest is None:
            await ctx.response_formatter.safe_reply(ctx.message, _NO_IDEAS_REPLY)
            logger.info(
                "fieldtheory_possible_no_ideas",
                extra={"cid": correlation_id, "ideas_path": str(ideas_path)},
            )
            return

        try:
            payload = await asyncio.to_thread(_read_json, newest)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "fieldtheory_possible_parse_failed",
                extra={
                    "cid": correlation_id,
                    "ideas_file": str(newest),
                    "error": str(exc),
                },
            )
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"Could not read ideas file. Error ID: {correlation_id}",
            )
            return

        nodes = _extract_nodes(payload)
        if not nodes:
            await ctx.response_formatter.safe_reply(ctx.message, _NO_NODES_REPLY)
            logger.info(
                "fieldtheory_possible_empty_payload",
                extra={"cid": correlation_id, "ideas_file": str(newest)},
            )
            return

        text = _format_reply(newest.name, nodes[: self._top_n], len(nodes))
        await ctx.response_formatter.safe_reply(ctx.message, text)
        logger.info(
            "fieldtheory_possible_served",
            extra={
                "cid": correlation_id,
                "ideas_file": str(newest),
                "nodes_total": len(nodes),
                "nodes_shown": min(len(nodes), self._top_n),
            },
        )


def _find_newest_ideas_file(ideas_path: pathlib.Path) -> pathlib.Path | None:
    if not ideas_path.is_dir():
        return None
    candidates = sorted(
        (p for p in ideas_path.rglob("*.json") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_nodes(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [n for n in payload if isinstance(n, dict)]
    if isinstance(payload, dict):
        for key in _NODES_CONTAINER_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return [n for n in value if isinstance(n, dict)]
    return []


def _format_reply(filename: str, nodes: list[dict[str, Any]], total: int) -> str:
    header = f"Latest Possible run: {filename} ({total} node{'s' if total != 1 else ''})"
    lines: list[str] = [header, ""]
    for idx, node in enumerate(nodes, start=1):
        title = _pick_first(node, _NODE_TITLE_KEYS) or f"node #{idx}"
        body = _pick_first(node, _NODE_BODY_KEYS)
        node_id = node.get("id") or node.get("node_id")
        suffix = f" (id: {node_id})" if node_id else ""
        lines.append(f"{idx}. {title}{suffix}")
        if body:
            snippet = body if len(body) <= 200 else body[:197] + "..."
            lines.append(f"   {snippet}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _pick_first(node: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


_NO_IDEAS_REPLY = (
    "No ideas yet. Run `ft possible run` on the host to generate ideas first; "
    "the bot only displays existing ideas, it does not generate them."
)
_NO_NODES_REPLY = (
    "The newest ideas file is present but contains no idea nodes. "
    "Try re-running `ft possible run` on the host."
)
