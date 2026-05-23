"""Shared configuration helpers for ratatoskr CLI entry points."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import AppConfig, load_config
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    import argparse

logger = get_logger(__name__)


def load_env_file(path: Path) -> None:
    """Load environment variables from a .env-style file if present."""
    if not path.exists() or not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def prepare_config(args: argparse.Namespace) -> AppConfig:
    """Load configuration, optionally applying CLI overrides for log_level."""
    base_dir = Path(__file__).resolve().parents[2]
    candidates: list[Path] = []
    if getattr(args, "env_file", None):
        candidates.append(args.env_file)
    else:
        candidates.extend([Path.cwd() / ".env", base_dir / ".env"])

    for candidate in candidates:
        try:
            load_env_file(candidate)
            if candidate.exists():
                logger.debug("loaded_env_file", extra={"path": str(candidate)})
        except Exception as exc:
            logger.warning("env_file_error", extra={"path": str(candidate), "error": str(exc)})
            continue

    try:
        cfg = load_config(allow_stub_telegram=True)
    except RuntimeError as exc:
        msg = (
            "Configuration error: "
            f"{exc}. Set FIRECRAWL_SELF_HOSTED_ENABLED=true (and OPENROUTER_API_KEY) before running the CLI."
        )
        raise SystemExit(msg) from exc

    runtime = cfg.runtime
    updated = False

    if getattr(args, "db_path", None):
        logger.warning(
            "cli_db_path_ignored",
            extra={"db_path": str(args.db_path), "reason": "postgresql_runtime"},
        )

    if getattr(args, "log_level", None):
        runtime = replace(runtime, log_level=args.log_level)
        updated = True

    if updated:
        cfg = replace(cfg, runtime=runtime)

    return cfg
