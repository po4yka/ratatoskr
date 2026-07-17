"""CLI: trigger an AI account backup on demand without waiting for the cron.

Usage:
    # Run a backup now (all enabled services, or one with --service):
    python -m app.cli.ai_backup [--service {chatgpt,claude}] [--log-level LEVEL]

    # Ingest a captured session blob straight into the encrypted store
    # (no JWT / REST needed — runs in-container with DB access):
    python -m app.cli.ai_backup --ingest PATH --service {chatgpt,claude}

Omit --service to run all currently-enabled services (mirrors the Taskiq cron
behaviour). Pass --service to force a single service even if its config flag is
off — useful for validating a freshly-supplied session before the next scheduled
window. ``--ingest`` validates the service-bound session cookie, stores it Fernet-encrypted
for the owner (first ALLOWED_USER_IDS), and marks authorization unverified.

Exit codes:
    0  success (or no services enabled)
    1  unexpected exception
    2  bad input (empty ALLOWED_USER_IDS, unreadable/invalid blob)
  130  interrupted by SIGINT
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from app.adapters.ai_backup.repository import AiBackupRepository
from app.adapters.ai_backup.service import AiBackupOrchestrationService, NullNotifier
from app.adapters.ai_backup.session_store import (
    AiBackupSessionStore,
    validate_storage_state,
)
from app.config import load_config
from app.core.logging_utils import get_logger
from app.db.models.ai_backup import AiBackupService
from app.db.session import Database

logger = get_logger(__name__)

_SERVICE_CHOICES: list[str] = [s.value for s in AiBackupService]


def _enabled_services(cfg: Any) -> list[AiBackupService]:
    """Return the services the operator has switched on — mirrors app.tasks.ai_backup_sync."""
    services: list[AiBackupService] = []
    if cfg.ai_backup.chatgpt_enabled:
        services.append(AiBackupService.CHATGPT)
    if cfg.ai_backup.claude_enabled:
        services.append(AiBackupService.CLAUDE)
    return services


async def run_backup(service_name: str | None) -> int:
    """Execute a backup run and print the resulting DB row for each service.

    Args:
        service_name: ``"chatgpt"`` / ``"claude"`` to target one service, or
            ``None`` to run all currently-enabled services.

    Returns:
        Exit code (0 on success, 2 if the owner cannot be determined).
    """
    cfg = load_config()
    db = Database(config=cfg.database)
    try:
        owner = next(iter(cfg.telegram.allowed_user_ids), None)
        if owner is None:
            print(
                "Error: ALLOWED_USER_IDS is empty; cannot determine backup owner.",
                file=sys.stderr,
            )
            return 2

        if service_name is not None:
            target = AiBackupService(service_name)
            flag = f"{target.value}_enabled"
            if not getattr(cfg.ai_backup, flag, False):
                print(
                    f"Warning: {target.value} is not enabled in config "
                    f"(AI_BACKUP_{target.value.upper()}_ENABLED is off); "
                    "running anyway for validation.",
                    file=sys.stderr,
                )
            services: list[AiBackupService] = [target]
        else:
            services = _enabled_services(cfg)
            if not services:
                print(
                    "No AI backup services are enabled. "
                    "Set AI_BACKUP_CHATGPT_ENABLED or AI_BACKUP_CLAUDE_ENABLED to run a backup.",
                    file=sys.stderr,
                )
                return 0

        repo = AiBackupRepository(db)
        store = AiBackupSessionStore(db)
        svc = AiBackupOrchestrationService(cfg, repo, store, notifier=NullNotifier())

        for service in services:
            print(f"--- Running backup for {service.value} ---")
            await svc.run(owner, service)
            row = await repo.get(owner, service)
            if row is None:
                print(f"  No database row found for {service.value}.")
            else:
                print(f"  backup_status:    {row.status.value}")
                print(f"  authorization:    {row.authorization_status.value}")
                print(f"  counts_json:      {row.counts_json}")
                print(f"  last_backup_path: {row.last_backup_path}")
                print(f"  last_error:       {row.last_error}")
    finally:
        await db.dispose()

    return 0


async def ingest_session(service_name: str, path: str) -> int:
    """Store a captured Playwright storage_state blob for the owner (no JWT/REST).

    Reads ``path``, validates the shape, encrypts + persists it for the first
    ALLOWED_USER_IDS owner, and marks authorization unverified. Prints cookie names
    only — never values.

    Returns exit code (0 on success; 2 on bad input).
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Error: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    service = AiBackupService(service_name)
    try:
        storage_state = json.loads(raw)
        validate_storage_state(service, storage_state)
    except json.JSONDecodeError as exc:
        print(f"Error: {path} is not valid JSON: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Error: invalid storage_state shape: {exc}", file=sys.stderr)
        return 2

    cfg = load_config()
    db = Database(config=cfg.database)
    try:
        owner = next(iter(cfg.telegram.allowed_user_ids), None)
        if owner is None:
            print(
                "Error: ALLOWED_USER_IDS is empty; cannot determine backup owner.",
                file=sys.stderr,
            )
            return 2
        await AiBackupSessionStore(db).save(owner, service, storage_state)
        await AiBackupRepository(db).mark_authorization_unverified(owner, service)
    finally:
        await db.dispose()

    cookies = storage_state.get("cookies") or []
    names = sorted({c.get("name", "?") for c in cookies if isinstance(c, dict)})
    print(f"Session for {service.value} stored for user {owner} ({len(cookies)} cookies).")
    print(f"  cookie names: {', '.join(names) if names else '(none)'}")
    print("  Delete the source file now — it contains live session cookies.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.ai_backup",
        description="Trigger an AI account backup on demand.",
    )
    parser.add_argument(
        "--service",
        choices=_SERVICE_CHOICES,
        default=None,
        metavar="{" + ",".join(_SERVICE_CHOICES) + "}",
        help=("Which service to back up. Omit to run all enabled services (default: all enabled)."),
    )
    parser.add_argument(
        "--ingest",
        metavar="PATH",
        default=None,
        help=(
            "Ingest a Playwright storage_state JSON file into the encrypted session "
            "store for --service, then exit (no JWT/REST needed). Requires --service."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO).",
    )
    args = parser.parse_args()
    if args.ingest is not None and args.service is None:
        parser.error("--ingest requires --service")

    logging.basicConfig(level=getattr(logging, args.log_level))

    try:
        if args.ingest is not None:
            return asyncio.run(ingest_session(args.service, args.ingest))
        return asyncio.run(run_backup(args.service))
    except KeyboardInterrupt:
        logger.info("ai_backup_cli_interrupted")
        return 130
    except Exception:
        logger.exception("ai_backup_cli_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
