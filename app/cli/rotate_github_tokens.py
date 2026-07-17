"""Re-encrypt GitHub tokens and browser sessions under the primary Fernet key.

Run this after adding a new GITHUB_TOKEN_ENCRYPTION_KEY and moving the old key to
GITHUB_TOKEN_PREVIOUS_KEYS. It rotates both GitHub integration tokens and every
stored browser session. Once complete, the old key can be safely removed.

Usage:
    python -m app.cli.rotate_github_tokens [--dry-run] [--user-id ID] [--log-level LEVEL]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.cli._runtime import prepare_config
from app.core.logging_utils import get_logger, setup_json_logging
from app.db.models.repository import UserGitHubIntegration
from app.db.models.webwright import UserBrowserSession
from app.di.database import build_runtime_database
from app.security.secret_crypto import decrypt_secret, encrypt_secret

if TYPE_CHECKING:
    from app.db.session import Database

logger = get_logger(__name__)

__all__ = ["ReencryptResult", "main", "reencrypt_all_tokens"]


@dataclass(frozen=True)
class ReencryptResult:
    processed: int
    reencrypted: int
    failed: int
    github_tokens_processed: int
    browser_sessions_processed: int


async def _reencrypt_rows(
    db: Database,
    *,
    rows: list[Any],
    model: type[Any],
    ciphertext_field: str,
    secret_kind: str,
    dry_run: bool,
) -> tuple[int, int, int]:
    processed = reencrypted = failed = 0
    for row in rows:
        processed += 1
        old_ciphertext = getattr(row, ciphertext_field)
        try:
            plaintext = decrypt_secret(old_ciphertext)
            new_ciphertext = encrypt_secret(plaintext)
            if not dry_run:
                async with db.transaction() as txn:
                    fresh = await txn.get(model, row.id)
                    if fresh is None or getattr(fresh, ciphertext_field) != old_ciphertext:
                        failed += 1
                        logger.warning(
                            "secret_reencrypt_skipped_stale_row",
                            extra={"user_id": row.user_id, "secret_kind": secret_kind},
                        )
                        continue
                    setattr(fresh, ciphertext_field, new_ciphertext)
                setattr(row, ciphertext_field, new_ciphertext)
            reencrypted += 1
            logger.info(
                "secret_reencrypted",
                extra={
                    "user_id": row.user_id,
                    "secret_kind": secret_kind,
                    "dry_run": dry_run,
                },
            )
        except ValueError:
            failed += 1
            logger.error(
                "secret_reencrypt_failed_undecryptable",
                extra={"user_id": row.user_id, "secret_kind": secret_kind},
            )
    return processed, reencrypted, failed


async def reencrypt_all_tokens(
    db: Database,
    *,
    dry_run: bool = False,
    user_id: int | None = None,
) -> ReencryptResult:
    """Re-encrypt every GitHub token and browser session with the primary key.

    Decryption uses MultiFernet (primary + previous keys); encryption uses primary
    only. Rows that cannot be decrypted or change concurrently are counted as
    *failed* and logged; the remaining rows continue.
    """
    async with db.session() as session:
        github_stmt = select(UserGitHubIntegration)
        browser_stmt = select(UserBrowserSession)
        if user_id is not None:
            github_stmt = github_stmt.where(UserGitHubIntegration.user_id == user_id)
            browser_stmt = browser_stmt.where(UserBrowserSession.user_id == user_id)
        github_rows = list((await session.execute(github_stmt)).scalars().all())
        browser_rows = list((await session.execute(browser_stmt)).scalars().all())

    github_counts = await _reencrypt_rows(
        db,
        rows=github_rows,
        model=UserGitHubIntegration,
        ciphertext_field="encrypted_token",
        secret_kind="github_token",
        dry_run=dry_run,
    )
    browser_counts = await _reencrypt_rows(
        db,
        rows=browser_rows,
        model=UserBrowserSession,
        ciphertext_field="encrypted_cookies",
        secret_kind="browser_session",
        dry_run=dry_run,
    )
    return ReencryptResult(
        processed=github_counts[0] + browser_counts[0],
        reencrypted=github_counts[1] + browser_counts[1],
        failed=github_counts[2] + browser_counts[2],
        github_tokens_processed=github_counts[0],
        browser_sessions_processed=browser_counts[0],
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Re-encrypt GitHub integration tokens and browser sessions under the "
            "primary Fernet key. Run after rotating GITHUB_TOKEN_ENCRYPTION_KEY."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report would-be changes without writing to the database.",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Restrict re-encryption to this Telegram user_id.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Path to a .env file with environment variables.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    cfg = prepare_config(args)
    setup_json_logging(cfg.runtime.log_level)

    db = build_runtime_database(cfg, migrate=False)
    result = await reencrypt_all_tokens(db, dry_run=args.dry_run, user_id=args.user_id)

    try:
        import orjson

        print(orjson.dumps(asdict(result), option=orjson.OPT_INDENT_2).decode())
    except ImportError:
        print(json.dumps(asdict(result), indent=2))

    if result.failed:
        sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m app.cli.rotate_github_tokens``."""
    args = parse_args(argv)
    try:
        asyncio.run(_run(args))
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1
    except KeyboardInterrupt:  # pragma: no cover
        return 1
    except Exception as exc:
        logger.exception("rotate_github_tokens_failed", exc_info=exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
