"""`/browse <task>` command — owner-only Webwright run.

Fires off a Microsoft Webwright agent loop against the user-supplied task,
persists the run in ``webwright_runs``, and replies with the final answer.
Access is already gated by ``ALLOWED_USER_IDS`` upstream (AccessController);
this handler does not need its own ownership check.

Each invocation gets a correlation_id that flows into the WebwrightClient via
the X-Correlation-Id header, so logs/trajectories join back to the Ratatoskr
request (Operating Rule 1). All failures (network, sidecar, timeout) land in
a webwright_runs row with status != "completed" so we can audit later.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING

import sqlalchemy as sa

from app.adapters.telegram.command_handlers.decorators import audit_command
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.db.models.webwright import WebwrightRun, WebwrightRunStatus

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )
    from app.adapters.webwright.client import WebwrightClient
    from app.db.session import Database

logger = get_logger(__name__)


class BrowseHandler:
    """Handle `/browse <natural-language task>`."""

    def __init__(
        self,
        *,
        db: Database,
        response_formatter: ResponseFormatter,
        webwright_client: WebwrightClient,
    ) -> None:
        self._db = db
        self._formatter = response_formatter
        self._client = webwright_client

    @audit_command("command_browse", include_text=True)
    async def handle_browse(self, ctx: CommandExecutionContext) -> tuple[str | None, bool]:
        task_text = self._extract_task(ctx.text)
        if not task_text:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "Usage: /browse <task>\n\n"
                "Example: /browse Go to news.ycombinator.com and "
                "summarize the top 3 stories.",
            )
            return "browse_usage", False

        run_id = await self._create_run(
            user_id=ctx.uid,
            correlation_id=ctx.correlation_id,
            task_text=task_text,
        )

        await ctx.response_formatter.safe_reply(
            ctx.message,
            (
                "Running browser agent... this can take up to a few minutes.\n"
                f"Run ID: {run_id}\nError ID: {ctx.correlation_id}"
            ),
        )

        result = await self._client.run_task(
            task=task_text,
            correlation_id=ctx.correlation_id,
            allowed_domains=(),
        )

        await self._finalize_run(
            run_id=run_id,
            result_status=result.status,
            steps_used=result.steps_used,
            llm_cost_usd=result.llm_cost_usd,
            final_answer=result.final_answer,
            trajectory_path=result.trajectory_path,
            screenshots=list(result.screenshots),
            error_text=result.error_text,
        )

        await ctx.response_formatter.safe_reply(
            ctx.message,
            self._format_reply(result, ctx.correlation_id),
        )
        return "browse_completed" if result.status == "ok" else "browse_error", False

    @staticmethod
    def _extract_task(text: str) -> str:
        # `/browse this is the task` -> "this is the task"
        stripped = text.strip()
        if not stripped:
            return ""
        if stripped.startswith("/browse"):
            stripped = stripped[len("/browse") :]
        return stripped.strip()

    async def _create_run(
        self, *, user_id: int, correlation_id: str, task_text: str
    ) -> int:
        async with self._db.session() as session:
            row = WebwrightRun(
                user_id=user_id,
                correlation_id=correlation_id,
                task_text=task_text,
                status=WebwrightRunStatus.RUNNING,
            )
            session.add(row)
            await session.flush()
            await session.commit()
            assert row.id is not None
            return row.id

    async def _finalize_run(
        self,
        *,
        run_id: int,
        result_status: str,
        steps_used: int | None,
        llm_cost_usd: float | None,
        final_answer: str | None,
        trajectory_path: str | None,
        screenshots: list[str],
        error_text: str | None,
    ) -> None:
        status_map = {
            "ok": WebwrightRunStatus.COMPLETED,
            "timeout": WebwrightRunStatus.TIMEOUT,
            "error": WebwrightRunStatus.ERROR,
        }
        terminal_status = status_map.get(result_status, WebwrightRunStatus.ERROR)
        async with self._db.session() as session:
            await session.execute(
                sa.update(WebwrightRun)
                .where(WebwrightRun.id == run_id)
                .values(
                    status=terminal_status,
                    steps_used=steps_used,
                    llm_cost_usd=llm_cost_usd,
                    final_answer=final_answer,
                    trajectory_path=trajectory_path,
                    screenshots_json=screenshots or None,
                    error_text=error_text,
                    completed_at=_dt.datetime.now(UTC),
                )
            )
            await session.commit()

    @staticmethod
    def _format_reply(result, correlation_id: str) -> str:  # type: ignore[no-untyped-def]
        if result.status == "ok" and result.final_answer:
            header = "Browser agent finished."
            cost_line = (
                f"\n(steps={result.steps_used}, cost=${result.llm_cost_usd:.4f})"
                if result.llm_cost_usd is not None and result.steps_used is not None
                else ""
            )
            # Telegram caps messages around 4096 chars; truncate generously.
            answer = (result.final_answer or "").strip()
            if len(answer) > 3500:
                answer = answer[:3500] + "\n\n[truncated; see trajectory]"
            return f"{header}{cost_line}\n\n{answer}"

        # Failures still carry Error ID so the user can report it.
        err = result.error_text or f"status={result.status}"
        return (
            f"Browser agent failed.\n\n{err}\n\nError ID: {correlation_id}"
        )
