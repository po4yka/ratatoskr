"""CLI tooling to exercise the /summary command flow locally."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.cli._runtime import prepare_config as _prepare_config
from app.core.logging_utils import generate_correlation_id, get_logger, setup_json_logging
from app.di.database import build_runtime_database
from app.di.shared import close_runtime_resources
from app.di.telegram import build_summary_cli_runtime

logger = get_logger(__name__)

__all__ = ["main", "run_summary_cli"]


@dataclass(slots=True)
class CLIChat:
    """Lightweight stand-in for Telegram chat metadata."""

    id: int = 0
    type: str = "cli"
    title: str | None = "CLI session"


@dataclass(slots=True)
class CLIUser:
    """Lightweight stand-in for Telegram user metadata."""

    id: int = 0
    is_bot: bool = False
    username: str = "cli-user"


class CLIMessage:
    """Message adapter that mimics the Telegram message interface for CLI usage."""

    def __init__(self, text: str, *, json_output_path: Path | None = None) -> None:
        self.text = text
        self.caption: str | None = None
        self.id = 0
        self.message_id = 0
        self.chat = CLIChat()
        self.from_user = CLIUser()
        self.entities: list[Any] = []
        self.caption_entities: list[Any] = []
        self.date = None
        self.forward_date = None
        self.forward_from_chat = None
        self.forward_from_message_id = None
        self._json_output_path = json_output_path
        self._last_json: dict[str, Any] | None = None

    async def reply_text(self, text: str, *, parse_mode: str | None = None) -> None:
        """Print reply text to stdout."""
        print(text, flush=True)

    async def reply_document(self, file_obj: Any, caption: str | None = None) -> None:
        """Print JSON attachment content or persist to file when requested."""
        with contextlib.suppress(Exception):
            file_obj.seek(0)
        data = file_obj.read()
        content = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)

        try:
            self._last_json = json.loads(content)
        except json.JSONDecodeError:
            self._last_json = None

        if self._json_output_path:
            self._json_output_path.parent.mkdir(parents=True, exist_ok=True)
            self._json_output_path.write_text(content, encoding="utf-8")
        sys.stdout.flush()

    def to_dict(self) -> dict[str, Any]:
        """Return a minimal dict representation for persistence helpers."""
        return {
            "text": self.text,
            "chat": {
                "id": self.chat.id,
                "type": self.chat.type,
                "title": self.chat.title,
            },
            "from_user": {
                "id": self.from_user.id,
                "is_bot": self.from_user.is_bot,
                "username": self.from_user.username,
            },
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Run the /summary command flow locally for testing",
        allow_abbrev=False,
    )
    parser.add_argument(
        "text",
        nargs="?",
        help="Full message text (e.g. '/summary https://example.com/article')",
    )
    parser.add_argument(
        "--url",
        help="Convenience shortcut; builds the message as '/summary <url>'.",
    )
    parser.add_argument(
        "--accept-multiple",
        action="store_true",
        help="Automatically process all URLs when multiple links are supplied.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        help="Deprecated; ignored by the PostgreSQL-backed runtime.",
    )
    parser.add_argument(
        "--json-path",
        type=Path,
        help="Write the final summary JSON to a file instead of stdout.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override the configured log level for this session.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Path to a .env file containing environment variables for the run.",
    )
    return parser.parse_args(argv)


def _resolve_text(args: argparse.Namespace) -> str:
    """Resolve the message text from positional and optional arguments."""
    if args.text and args.url:
        msg = "Specify either a positional message text or --url, not both."
        raise SystemExit(msg)

    if args.url:
        return f"/summary {args.url.strip()}"

    if args.text:
        return args.text

    msg = "Provide a message text or use --url to supply a link to summarize."
    raise SystemExit(msg)


async def run_summary_cli(args: argparse.Namespace) -> None:
    """Execute the /summary flow based on parsed CLI arguments."""
    text = _resolve_text(args)
    cfg = _prepare_config(args)

    setup_json_logging(cfg.runtime.log_level)

    db = build_runtime_database(cfg, migrate=False)
    await db.migrate()
    runtime = build_summary_cli_runtime(cfg, db)

    message = CLIMessage(text=text, json_output_path=args.json_path)

    correlation_id = generate_correlation_id()
    logger.info("cli_summary_start", extra={"cid": correlation_id})

    try:
        _next_action, _ = await runtime.command_processor.handle_summarize_command(
            message=message,
            text=text,
            uid=message.from_user.id,
            correlation_id=correlation_id,
            interaction_id=0,
            start_time=time.time(),
        )

    finally:
        await close_runtime_resources(
            runtime.url_processor,
            runtime.search.vector_store,
            runtime.search.embedding_service,
            runtime.core.firecrawl_client,
            runtime.core.llm_client,
        )


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m app.cli.summary``."""
    args = parse_args(argv)
    try:
        asyncio.run(run_summary_cli(args))
    except KeyboardInterrupt:  # pragma: no cover - user cancelled
        return 1
    except Exception as exc:
        logger.exception("cli_summary_failed", exc_info=exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
