"""Startup validator for the Telethon userbot SQLite session schema.

Telethon changed the version-table column name from ``number`` (older releases)
to ``version`` (current releases).  A session file created with the old schema
will crash on every digest tick with::

    sqlite3.OperationalError: no such column: version

This module probes the session file at startup and auto-repairs the schema via
``ALTER TABLE version RENAME COLUMN number TO version`` when needed.

Lives in ``app/core`` (stdlib-only, no adapter dependencies) so it can be
imported by both ``app/adapters/telegram`` and ``app/adapters/digest`` without
creating a cross-adapter import cycle.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

_EXPECTED_COLUMN = "version"
_LEGACY_COLUMN = "number"


def validate_and_repair_session(session_file: Path) -> dict[str, str]:
    """Check and optionally repair the Telethon session schema.

    Returns a result dict with key ``status``:
    - ``"absent"``       -file does not exist (no action taken).
    - ``"ok"``           -schema is correct, nothing to do.
    - ``"repaired"``     -legacy column renamed to ``version``; key ``from`` holds old name.
    - ``"incompatible"`` -neither expected column found; key ``columns`` lists what was found.
    - ``"error"``        -sqlite3 operation failed; key ``error`` holds the message.
    """
    if not session_file.exists():
        return {"status": "absent"}

    try:
        with sqlite3.connect(session_file) as conn:
            cursor = conn.execute("PRAGMA table_info(version)")
            columns = {row[1] for row in cursor.fetchall()}

        if _EXPECTED_COLUMN in columns:
            logger.info("digest_session_schema_ok", extra={"file": str(session_file)})
            return {"status": "ok"}

        if _LEGACY_COLUMN in columns:
            with sqlite3.connect(session_file) as conn:
                conn.execute(
                    f"ALTER TABLE version RENAME COLUMN {_LEGACY_COLUMN} TO {_EXPECTED_COLUMN}"
                )
                conn.commit()
            logger.info(
                "digest_session_schema_repaired",
                extra={"file": str(session_file), "from": _LEGACY_COLUMN},
            )
            return {"status": "repaired", "from": _LEGACY_COLUMN}

        logger.warning(
            "digest_session_schema_incompatible",
            extra={"file": str(session_file), "columns": ",".join(sorted(columns))},
        )
        return {"status": "incompatible", "columns": ",".join(sorted(columns))}

    except sqlite3.Error as exc:
        logger.error(
            "digest_session_schema_error",
            extra={"file": str(session_file), "error": str(exc)},
        )
        return {"status": "error", "error": str(exc)}
