from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from app.config.database import DatabaseConfig
from app.db.models import Request
from app.db.session import Database

if TYPE_CHECKING:
    from pathlib import Path


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


def _docker_postgres_available() -> bool:
    result = subprocess.run(
        ["docker", "inspect", "ratatoskr-postgres"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_runtime_services_against_postgres(tmp_path: Path) -> None:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for runtime service smoke test")
    if not _docker_postgres_available():
        pytest.skip("ratatoskr-postgres container is required for runtime service smoke test")

    _reset_database(_database_name(dsn))
    database = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    try:
        await database.migrate()
        async with database.transaction() as session:
            await session.execute(delete(Request).where(Request.dedupe_hash == "runtime-service"))
            request = Request(type="url", status="done", dedupe_hash="runtime-service")
            session.add(request)

        ok, reason = await database.inspection.async_check_integrity()
        assert (ok, reason) == (True, "ok")

        overview = await database.inspection.async_get_database_overview()
        assert overview["tables"]["requests"] >= 1
        assert overview["total_requests"] >= 1
        assert await database.inspection.async_database_size_mb() > 0
        verification = await database.inspection.async_verify_processing_integrity(limit=1000)
        assert verification["overview"]["total_requests"] >= 1

        database.maintenance.run_startup_maintenance()
        assert await database.maintenance.async_run_analyze() is True
        stats = await database.maintenance.async_get_database_stats()
        assert stats["type"] == "postgres"
        assert stats["size_bytes"] > 0

        async def _load_request(session, dedupe_hash: str) -> int | None:
            return await session.scalar(
                select(Request.id).where(Request.dedupe_hash == dedupe_hash)
            )

        request_id = await database.executor.async_execute(_load_request, "runtime-service")
        assert isinstance(request_id, int)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_inspection_reports_bad_dsn() -> None:
    database = Database(
        DatabaseConfig(
            dsn="postgresql+asyncpg://ratatoskr_app:bad@127.0.0.1:1/ratatoskr",
            pool_size=1,
            max_overflow=1,
        )
    )
    try:
        ok, reason = await database.inspection.async_check_integrity()
        assert ok is False
        assert reason
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_backup_dump_round_trips_with_pg_restore(tmp_path: Path) -> None:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for backup smoke test")
    if not _docker_postgres_available():
        pytest.skip("ratatoskr-postgres container is required for pg_dump/pg_restore smoke test")

    _reset_database(_database_name(dsn))
    restore_db = "ratatoskr_runtime_restore"
    database = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    try:
        await database.migrate()
        async with database.transaction() as session:
            await session.execute(delete(Request).where(Request.dedupe_hash == "runtime-backup"))
            session.add(Request(type="url", status="done", dedupe_hash="runtime-backup"))

        dump_path = database.backups.create_backup_copy(str(tmp_path / "runtime.dump"))
        assert dump_path.exists()
        assert dump_path.stat().st_size > 0

        _reset_restore_database(restore_db)
        with dump_path.open("rb") as dump_file:
            subprocess.run(  # noqa: ASYNC221
                [
                    "docker",
                    "exec",
                    "-i",
                    "ratatoskr-postgres",
                    "pg_restore",
                    "-U",
                    "ratatoskr_app",
                    "-d",
                    restore_db,
                    "--clean",
                    "--if-exists",
                ],
                check=True,
                stdin=dump_file,
            )
        source_count = _table_count("ratatoskr", "requests")
        restored_count = _table_count(restore_db, "requests")
        assert restored_count == source_count
    finally:
        _drop_restore_database(restore_db)
        await database.dispose()


def _reset_restore_database(name: str) -> None:
    _reset_database(name)


def _reset_database(name: str) -> None:
    _drop_restore_database(name)
    subprocess.run(
        ["docker", "exec", "ratatoskr-postgres", "createdb", "-U", "ratatoskr_app", name],
        check=True,
    )


def _drop_restore_database(name: str) -> None:
    subprocess.run(
        [
            "docker",
            "exec",
            "ratatoskr-postgres",
            "dropdb",
            "-U",
            "ratatoskr_app",
            "--if-exists",
            "--force",
            name,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _database_name(dsn: str) -> str:
    database = make_url(dsn).database
    assert database is not None
    return database


def _table_count(database: str, table: str) -> int:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "ratatoskr-postgres",
            "psql",
            "-U",
            "ratatoskr_app",
            "-d",
            database,
            "-tAc",
            f"SELECT COUNT(*) FROM {table}",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return int(result.stdout.strip())
