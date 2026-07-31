"""Database bootstrap and migration services."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config


class DatabaseBootstrapService:
    """Run schema bootstrap for the SQLAlchemy/Postgres runtime."""

    def __init__(self, *, dsn: str, logger: Any) -> None:
        self._dsn = dsn
        self._logger = logger

    def migrate(self) -> None:
        ini_path = Path(__file__).resolve().parents[3] / "alembic.ini"
        cfg = Config(str(ini_path))
        cfg.set_main_option("sqlalchemy.url", self._dsn)
        command.upgrade(cfg, "head")
        self._logger.info("db_migrated", extra={"database": self._mask_dsn(self._dsn)})

    @staticmethod
    def _mask_dsn(dsn: str) -> str:
        if "@" not in dsn:
            return dsn
        prefix, suffix = dsn.rsplit("@", 1)
        scheme = prefix.split("://", 1)[0]
        return f"{scheme}://...@{suffix}"
