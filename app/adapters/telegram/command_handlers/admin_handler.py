"""Admin/maintenance command handlers (/admin, /dbinfo, /dbverify).

This module handles administrative commands for database inspection
and verification, including automated reprocessing of failed requests.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Any

from app.adapters.telegram.command_handlers.decorators import audit_command
from app.api.services.admin_read_service import AdminReadService
from app.core.logging_utils import generate_correlation_id, get_logger
from app.core.time_utils import UTC
from app.db.user_interactions import async_safe_update_user_interaction

if TYPE_CHECKING:
    from app.adapters.content.url_processor import URLProcessor
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )
    from app.adapters.telegram.url_handler import URLHandler
    from app.db.session import Database

logger = get_logger(__name__)


class AdminHandler:
    """Implementation of admin/maintenance commands (/admin, /dbinfo, /dbverify).

    These commands provide database inspection and verification capabilities
    for the bot owner to monitor system health and data integrity.
    """

    def __init__(
        self,
        db: Database,
        response_formatter: ResponseFormatter,
        url_processor: URLProcessor,
        url_handler: URLHandler | None = None,
        cfg: Any = None,
    ) -> None:
        self._db = db
        self._cfg = cfg
        self._formatter = response_formatter
        self._url_processor = url_processor
        self._url_handler = url_handler

    @audit_command("command_admin")
    async def handle_admin(self, ctx: CommandExecutionContext) -> None:
        """Handle /admin command with subcommands.

        Subcommands:
            (none) - Show overview stats (users, summaries, requests).
            jobs   - Show background job / pipeline status.
            errors - Show recent error summary (last 24h).

        Args:
            ctx: The command execution context.
        """
        subcommand = self._parse_admin_subcommand(ctx.text)

        try:
            if subcommand == "jobs":
                reply = await self._build_jobs_reply()
            elif subcommand == "errors":
                reply = await self._build_errors_reply()
            else:
                reply = await self._build_overview_reply()
        except Exception as exc:
            logger.exception("command_admin_failed", extra={"cid": ctx.correlation_id})
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "Unable to fetch admin stats right now. Check bot logs for details.",
            )
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="admin_error",
                    error_occurred=True,
                    error_message=str(exc)[:500],
                    start_time=ctx.start_time,
                    logger_=logger,
                )
            return

        await ctx.response_formatter.safe_reply(ctx.message, reply)

        if ctx.interaction_id:
            await async_safe_update_user_interaction(
                ctx.user_repo,
                interaction_id=ctx.interaction_id,
                response_sent=True,
                response_type=f"admin_{subcommand or 'overview'}",
                start_time=ctx.start_time,
                logger_=logger,
            )

    # ------------------------------------------------------------------
    # /admin subcommand helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_admin_subcommand(text: str) -> str | None:
        """Extract the subcommand token after ``/admin``.

        Returns ``None`` when no recognised subcommand is present.
        """
        parts = text.strip().split(maxsplit=1)
        if len(parts) < 2:
            return None
        sub = parts[1].strip().lower()
        if sub in ("jobs", "errors"):
            return sub
        return None

    async def _build_overview_reply(self) -> str:
        """Build the default /admin overview message."""
        now = _dt.datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        admin = AdminReadService(self._db)
        users = await admin.list_users()
        health = await admin.content_health()
        jobs = await admin.job_status(today=today_start)

        return (
            "Admin Overview:\n"
            f"Users: {int(users.get('total_users') or 0):,}\n"
            f"Total summaries: {int(health.get('total_summaries') or 0):,}\n"
            f"Total requests: {int(health.get('total_requests') or 0):,}\n"
            f"Pending requests: {int(jobs.get('pipeline', {}).get('pending') or 0):,}\n"
            f"Failed today: {int(jobs.get('pipeline', {}).get('failed_today') or 0):,}"
        )

    async def _build_jobs_reply(self) -> str:
        """Build the /admin jobs message."""
        now = _dt.datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        data = await AdminReadService(self._db).job_status(today=today_start)
        pipeline = data.get("pipeline", {})
        imports = data.get("imports", {})

        return (
            "Pipeline Status:\n"
            f"Pending: {int(pipeline.get('pending') or 0)} | "
            f"Processing: {int(pipeline.get('processing') or 0)}\n"
            f"Completed today: {int(pipeline.get('completed_today') or 0)} | "
            f"Failed: {int(pipeline.get('failed_today') or 0)}\n"
            "\n"
            "Import Jobs:\n"
            f"Active: {int(imports.get('active') or 0)} | "
            f"Completed today: {int(imports.get('completed_today') or 0)}"
        )

    async def _build_errors_reply(self) -> str:
        """Build the /admin errors message."""
        health = await AdminReadService(self._db).content_health()
        error_rows = health.get("failed_by_error_type", {})
        if not error_rows:
            return "Recent Errors (last 24h):\nNo errors recorded."

        lines = ["Recent Errors (last 24h):"]
        for label, count in sorted(error_rows.items(), key=lambda item: item[1], reverse=True):
            lines.append(f"{label}: {count}")

        failures = health.get("recent_failures", [])
        latest = failures[0] if isinstance(failures, list) and failures else None
        if isinstance(latest, dict):
            lines.append("")
            lines.append("Latest failure:")
            lines.append(f"URL: {latest.get('url') or 'N/A'}")
            lines.append(f"Error: {latest.get('error_message') or 'N/A'}")

        return "\n".join(lines)

    @audit_command("command_dbinfo")
    async def handle_dbinfo(self, ctx: CommandExecutionContext) -> None:
        """Handle /dbinfo command.

        Retrieves and displays a database overview including table counts,
        request statistics, and storage information.

        Args:
            ctx: The command execution context.
        """
        try:
            overview = await self._db.inspection.async_get_database_overview()
        except Exception as exc:
            logger.exception("command_dbinfo_failed", extra={"cid": ctx.correlation_id})
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "⚠️ Unable to read database overview right now. Check bot logs for details.",
            )
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="dbinfo_error",
                    error_occurred=True,
                    error_message=str(exc)[:500],
                    start_time=ctx.start_time,
                    logger_=logger,
                )
            return

        await self._formatter.send_db_overview(ctx.message, overview)

        if ctx.interaction_id:
            await async_safe_update_user_interaction(
                ctx.user_repo,
                interaction_id=ctx.interaction_id,
                response_sent=True,
                response_type="dbinfo",
                start_time=ctx.start_time,
                logger_=logger,
            )

    @audit_command("command_dbverify")
    async def handle_dbverify(self, ctx: CommandExecutionContext) -> None:
        """Handle /dbverify command.

        Verifies database integrity by checking for:
        - Missing summaries for completed requests
        - Invalid summary JSON structures
        - Missing crawl results

        If issues are found, offers to reprocess affected URLs.

        Args:
            ctx: The command execution context.
        """
        try:
            # Limit verification to the last 1000 records to prevent memory exhaustion
            # and ensure the command remains responsive.
            verification = await self._db.inspection.async_verify_processing_integrity(limit=1000)
        except Exception as exc:
            logger.exception("command_dbverify_failed", extra={"cid": ctx.correlation_id})
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "⚠️ Unable to verify database records right now. Check bot logs for details.",
            )
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="dbverify_error",
                    error_occurred=True,
                    error_message=str(exc)[:500],
                    start_time=ctx.start_time,
                    logger_=logger,
                )
            return

        # Send verification results
        await self._formatter.send_db_verification(ctx.message, verification)

        # Process reprocessing entries
        await self._process_reprocess_entries(ctx, verification)

        if ctx.interaction_id:
            await async_safe_update_user_interaction(
                ctx.user_repo,
                interaction_id=ctx.interaction_id,
                response_sent=True,
                response_type="dbverify",
                start_time=ctx.start_time,
                logger_=logger,
            )

    async def handle_setmodel(self, ctx: CommandExecutionContext) -> None:
        """Handle /setmodel <section> <model_name> command."""
        try:
            parts = (ctx.text or "").strip().split(None, 2)
            if len(parts) < 3:
                from app.config.config_file import SECTION_MAP as _SECTION_MAP

                valid = ", ".join(sorted(_SECTION_MAP))
                await ctx.response_formatter.safe_reply(
                    ctx.message,
                    f"Usage: /setmodel <section> <model>\nSections: {valid}",
                )
                return

            _, section, new_model = parts

            from app.config._validators import validate_model_name

            try:
                validate_model_name(new_model)
            except ValueError as exc:
                await ctx.response_formatter.safe_reply(ctx.message, f"Invalid model: {exc}")
                return

            from app.config.config_file import save_model_to_yaml

            old_value, new_value = save_model_to_yaml(section, new_model)

            # Trigger config reload
            cfg_holder = self._cfg
            if hasattr(cfg_holder, "_cfg"):
                from app.config.config_holder import ConfigReloader

                reloader = ConfigReloader(cfg_holder)
                reloader.reload_now()

            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"Model updated:\n"
                f"  Section: <code>{section}</code>\n"
                f"  Old: <code>{old_value or 'unset'}</code>\n"
                f"  New: <code>{new_value}</code>",
                parse_mode="HTML",
            )
        except (ValueError, FileNotFoundError) as exc:
            await ctx.response_formatter.safe_reply(ctx.message, str(exc))
        except Exception:
            logger.exception("command_setmodel_failed", extra={"cid": ctx.correlation_id})
            await ctx.response_formatter.safe_reply(ctx.message, "Failed to update model.")

    async def handle_models(self, ctx: CommandExecutionContext) -> None:
        """Handle /models command: show active model configuration."""
        try:
            cfg = self._cfg
            or_cfg = cfg.openrouter
            rt_cfg = cfg.model_routing
            att_cfg = cfg.attachment

            lines = [
                "<b>Active Model Configuration</b>",
                "",
                "<b>OpenRouter</b>",
                f"  Primary: <code>{or_cfg.model}</code>",
                f"  Fallbacks: <code>{', '.join(or_cfg.fallback_models)}</code>",
                f"  Flash: <code>{or_cfg.flash_model}</code>",
                f"  Flash fallbacks: <code>{', '.join(or_cfg.flash_fallback_models)}</code>",
                "",
                "<b>Content Routing</b>",
                f"  Enabled: {rt_cfg.enabled}",
                f"  Default: <code>{rt_cfg.default_model}</code>",
                f"  Technical: <code>{rt_cfg.technical_model}</code>",
                f"  Sociopolitical: <code>{rt_cfg.sociopolitical_model}</code>",
                f"  Long context: <code>{rt_cfg.long_context_model}</code>",
                f"  Threshold: {rt_cfg.long_context_threshold_tokens:,} tokens",
                "",
                "<b>Vision</b>",
                f"  Model: <code>{att_cfg.vision_model}</code>",
            ]
            await ctx.response_formatter.safe_reply(
                ctx.message, "\n".join(lines), parse_mode="HTML"
            )
        except Exception:
            logger.exception("command_models_failed", extra={"cid": ctx.correlation_id})
            await ctx.response_formatter.safe_reply(
                ctx.message, "Failed to retrieve model configuration."
            )

    async def handle_clearcache(self, ctx: CommandExecutionContext) -> None:
        """Handle /clearcache command."""
        try:
            if self._url_handler is None:
                msg = "URL handler is unavailable"
                raise RuntimeError(msg)
            count = await self._url_handler.clear_extraction_cache()
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"✅ Cache cleared. Removed {count} keys.",
            )
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="cache_cleared",
                    start_time=ctx.start_time,
                    logger_=logger,
                )
        except Exception as exc:
            logger.error("cache_clear_failed", extra={"error": str(exc), "uid": ctx.uid})
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"❌ Failed to clear cache: {exc}",
            )
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="error",
                    error_occurred=True,
                    error_message=str(exc),
                    start_time=ctx.start_time,
                    logger_=logger,
                )

    async def _process_reprocess_entries(
        self,
        ctx: CommandExecutionContext,
        verification: dict[str, Any],
    ) -> None:
        """Process entries that need reprocessing.

        Args:
            ctx: The command execution context.
            verification: The verification result dictionary.
        """
        posts_info = verification.get("posts") if isinstance(verification, dict) else None
        reprocess_entries = posts_info.get("reprocess") if isinstance(posts_info, dict) else []

        if not reprocess_entries:
            return

        urls_to_process: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for entry in reprocess_entries:
            if not isinstance(entry, dict):
                continue

            req_type = str(entry.get("type") or "").lower()
            if req_type == "url":
                url = entry.get("normalized_url") or entry.get("input_url")
                if not url:
                    skipped.append(entry)
                    continue
                urls_to_process.append(
                    {
                        "request_id": entry.get("request_id"),
                        "url": url,
                        "reasons": entry.get("reasons") or [],
                    }
                )
            else:
                skipped.append(entry)

        if urls_to_process or skipped:
            await self._formatter.send_db_reprocess_start(
                ctx.message, url_targets=urls_to_process, skipped=skipped
            )

        # Reprocess each URL
        failures: list[dict[str, Any]] = []
        for target in urls_to_process:
            url = target["url"]
            req_id = target.get("request_id")
            per_link_cid = generate_correlation_id()

            logger.info(
                "dbverify_reprocess_start",
                extra={
                    "request_id": req_id,
                    "url": url,
                    "cid": per_link_cid,
                    "cid_parent": ctx.correlation_id,
                },
            )

            try:
                if self._url_handler is not None:
                    await self._url_handler.handle_single_url(
                        message=ctx.message,
                        url=url,
                        correlation_id=per_link_cid,
                        interaction_id=ctx.interaction_id,
                    )
                else:
                    from app.adapters.content.url_flow_models import URLFlowRequest

                    await self._url_processor.handle_url_flow(
                        URLFlowRequest(
                            message=ctx.message, url_text=url, correlation_id=per_link_cid
                        )
                    )
            except Exception as exc:
                logger.exception(
                    "dbverify_reprocess_failed",
                    extra={
                        "request_id": req_id,
                        "url": url,
                        "cid": per_link_cid,
                        "cid_parent": ctx.correlation_id,
                    },
                )
                failure_entry = dict(target)
                failure_entry["error"] = str(exc)
                failures.append(failure_entry)

        if urls_to_process or skipped:
            await self._formatter.send_db_reprocess_complete(
                ctx.message,
                url_targets=urls_to_process,
                failures=failures,
                skipped=skipped,
            )
