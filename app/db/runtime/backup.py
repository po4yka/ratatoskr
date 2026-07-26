"""Database backup service."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url


class DatabaseBackupService:
    """Create PostgreSQL custom-format backup dumps."""

    def __init__(self, *, dsn: str, logger: Any) -> None:
        self._dsn = dsn
        self._logger = logger

    def create_backup_copy(self, dest_path: str) -> Path:
        destination = Path(dest_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.suffix != ".dump":
            destination = destination.with_suffix(".dump")

        if shutil.which("pg_dump"):
            self._run_host_pg_dump(destination)
        else:
            self._run_docker_pg_dump(destination)

        self._logger.info("db_backup_dump_created", extra={"dest": self._mask_path(destination)})
        return destination

    def _run_host_pg_dump(self, destination: Path) -> None:
        url = make_url(self._dsn)
        command_dsn = url.set(drivername="postgresql", password=None).render_as_string(
            hide_password=False
        )
        env = os.environ.copy()
        if url.password:
            env["PGPASSWORD"] = url.password
        command = [
            "pg_dump",
            "--format=custom",
            f"--file={destination}",
            f"--dbname={command_dsn}",
        ]
        subprocess.run(command, check=True, env=env)

    def _run_docker_pg_dump(self, destination: Path) -> None:
        container = os.getenv("RATATOSKR_PGDUMP_DOCKER_CONTAINER", "ratatoskr-postgres")
        url = make_url(self._dsn)
        database = url.database or "ratatoskr"
        username = url.username or "ratatoskr_app"
        password = url.password or ""
        container_dsn = f"postgresql://{username}@127.0.0.1:5432/{database}"
        command = [
            "docker",
            "exec",
            "-e",
            "PGPASSWORD",
            container,
            "pg_dump",
            "--format=custom",
            f"--dbname={container_dsn}",
        ]
        env = os.environ.copy()
        env["PGPASSWORD"] = password
        with destination.open("wb") as dump_file:
            subprocess.run(command, check=True, env=env, stdout=dump_file)

    @staticmethod
    def _mask_path(path: Path) -> str:
        try:
            parent = path.parent.name
            return f".../{parent}/{path.name}" if parent else path.name
        except (OSError, ValueError, AttributeError):
            return "..."
