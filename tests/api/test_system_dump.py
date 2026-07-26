from __future__ import annotations

import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.api.routers.auth.tokens import create_access_token
from app.api.services.system_maintenance_service import DatabaseDumpFile
from app.config import Config
from app.db.models import User
from app.security.rate_limiter import RateLimitConfig, UserRateLimiter


def _allowed_user_id() -> int:
    return int(Config.get_allowed_user_ids()[0])


def _headers(user_id: int, *, client_id: str = "test_client") -> dict[str, str]:
    token = create_access_token(user_id, client_id=client_id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _reset_db_dump_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate external dump I/O and rate state while preserving endpoint behavior."""
    limiter = UserRateLimiter(RateLimitConfig(max_requests=3, window_seconds=3600))
    monkeypatch.setattr("app.api.routers.system._db_dump_local_limiter", limiter)

    def _fake_create_backup(self, *, backup_path: str, user_id: int) -> None:
        del self, user_id
        Path(backup_path).write_bytes(b"fake-postgres-dump")

    monkeypatch.setattr(
        "app.api.services.system_maintenance_service.SystemMaintenanceService._create_backup",
        _fake_create_backup,
    )


# ---------------------------------------------------------------------------
# db-dump endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_dump_get_returns_file(client: TestClient, db, user_factory):
    user = await user_factory(
        telegram_user_id=_allowed_user_id(), username="test_dump_user", is_owner=True
    )
    headers = _headers(user.telegram_user_id)

    response = client.get("/v1/system/db-dump", headers=headers)

    assert response.status_code == 200
    assert "content-length" in response.headers
    assert len(response.content) > 0


@pytest.mark.asyncio
async def test_db_dump_head_returns_headers(client: TestClient, db, user_factory):
    user = await user_factory(
        telegram_user_id=_allowed_user_id(), username="test_dump_head", is_owner=True
    )
    headers = _headers(user.telegram_user_id)

    response = client.head("/v1/system/db-dump", headers=headers)

    assert response.status_code == 200
    assert "content-length" in response.headers
    assert response.headers["accept-ranges"] == "bytes"


@pytest.mark.asyncio
async def test_db_dump_range_request_returns_partial_content(client: TestClient, db, user_factory):
    user = await user_factory(
        telegram_user_id=_allowed_user_id(), username="test_dump_range", is_owner=True
    )
    headers = {**_headers(user.telegram_user_id), "Range": "bytes=0-9"}

    response = client.get("/v1/system/db-dump", headers=headers)

    assert response.status_code == 206
    assert len(response.content) == 10
    assert response.headers["content-range"].startswith("bytes 0-9/")


@pytest.mark.asyncio
async def test_db_dump_file_is_cleaned_up_after_response(client: TestClient, db, user_factory):
    """Temp dump file must be deleted once the response is fully sent."""
    user = await user_factory(
        telegram_user_id=_allowed_user_id(), username="test_dump_cleanup", is_owner=True
    )
    headers = _headers(user.telegram_user_id)

    created_paths: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def capturing_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created_paths.append(path)
        return fd, path

    with patch(
        "app.api.services.system_maintenance_service.tempfile.mkstemp",
        side_effect=capturing_mkstemp,
    ):
        response = client.get("/v1/system/db-dump", headers=headers)

    assert response.status_code == 200
    assert len(created_paths) == 1
    assert not Path(created_paths[0]).exists(), "Dump file was not deleted after response"


@pytest.mark.asyncio
async def test_db_dump_uses_unique_path_per_request(client: TestClient, db, user_factory):
    """Two consecutive requests must not share the same temp file."""
    user = await user_factory(
        telegram_user_id=_allowed_user_id(), username="test_dump_unique", is_owner=True
    )
    headers = _headers(user.telegram_user_id)

    created_paths: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def capturing_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created_paths.append(path)
        return fd, path

    with patch(
        "app.api.services.system_maintenance_service.tempfile.mkstemp",
        side_effect=capturing_mkstemp,
    ):
        client.get("/v1/system/db-dump", headers=headers)
        client.get("/v1/system/db-dump", headers=headers)

    assert len(created_paths) == 2
    assert created_paths[0] != created_paths[1], "Both requests must use distinct temp paths"


@pytest.mark.asyncio
async def test_db_dump_requires_owner(client: TestClient, db, user_factory):
    non_owner = await user_factory(
        telegram_user_id=_allowed_user_id(), username="normal_user_dump", is_owner=False
    )
    headers = _headers(non_owner.telegram_user_id, client_id="test")

    response = client.get("/v1/system/db-dump", headers=headers)

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_db_dump_path_is_not_fixed_predictable_name(client: TestClient, db, user_factory):
    """Generated file must not be the old hardcoded ratatoskr_backup.dump."""
    user = await user_factory(
        telegram_user_id=_allowed_user_id(), username="test_dump_name", is_owner=True
    )
    headers = _headers(user.telegram_user_id)

    created_paths: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def capturing_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created_paths.append(path)
        return fd, path

    with patch(
        "app.api.services.system_maintenance_service.tempfile.mkstemp",
        side_effect=capturing_mkstemp,
    ):
        client.get("/v1/system/db-dump", headers=headers)

    assert created_paths
    assert os.path.basename(created_paths[0]) != "ratatoskr_backup.dump"


# ---------------------------------------------------------------------------
# db-dump rate limiting (atomic reservation, no TOCTOU race)
# ---------------------------------------------------------------------------


def _patch_fast_dump():
    """Patch build_db_dump_file so rate-limit tests don't shell out to pg_dump."""

    def _fake_build(self, *, user_id: int) -> DatabaseDumpFile:
        fd, path = tempfile.mkstemp(prefix="ratatoskr_dump_rl_test_", suffix=".dump")
        os.close(fd)
        with open(path, "wb") as fh:
            fh.write(b"fake-dump")
        return DatabaseDumpFile(path=path, filename="fake.dump")

    return patch(
        "app.api.services.system_maintenance_service.SystemMaintenanceService.build_db_dump_file",
        new=_fake_build,
    )


@pytest.mark.asyncio
async def test_db_dump_rate_limit_blocks_fourth_request_within_hour(
    client: TestClient, db, user_factory
):
    """Owner may run at most 3 db-dumps per rolling hour; the 4th must be rejected."""
    user = await user_factory(
        telegram_user_id=_allowed_user_id(), username="rl_seq_user", is_owner=True
    )
    headers = _headers(user.telegram_user_id)

    with _patch_fast_dump():
        for _ in range(3):
            response = client.get("/v1/system/db-dump", headers=headers)
            assert response.status_code == 200

        blocked = client.get("/v1/system/db-dump", headers=headers)

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_db_dump_rate_limit_shared_between_get_and_head(client: TestClient, db, user_factory):
    """HEAD probes must count against the same hourly budget as GET downloads."""
    user = await user_factory(
        telegram_user_id=_allowed_user_id(), username="rl_head_user", is_owner=True
    )
    headers = _headers(user.telegram_user_id)

    with _patch_fast_dump():
        assert client.get("/v1/system/db-dump", headers=headers).status_code == 200
        assert client.head("/v1/system/db-dump", headers=headers).status_code == 200
        assert client.get("/v1/system/db-dump", headers=headers).status_code == 200

        blocked_head = client.head("/v1/system/db-dump", headers=headers)

    assert blocked_head.status_code == 429


@pytest.mark.asyncio
async def test_db_dump_rate_limit_survives_concurrent_requests(
    client: TestClient, db, user_factory
):
    """Concurrent requests must not bypass the cap via the TOCTOU race.

    The bug this guards against: counting completed dumps via an
    after-the-fact audit-log row (written fire-and-forget once pg_dump
    finishes) lets many concurrent requests all read the same stale count
    and all pass the check. The fix reserves a slot atomically before
    pg_dump ever runs, so firing far more than 3 requests at once must
    still only let 3 of them reach the dump.
    """
    user = await user_factory(
        telegram_user_id=_allowed_user_id(), username="rl_concurrent_user", is_owner=True
    )
    headers = _headers(user.telegram_user_id)

    call_lock = threading.Lock()
    calls: list[int] = []

    def _fake_build(self, *, user_id: int) -> DatabaseDumpFile:
        # A brief pause widens the window during which overlapping requests
        # would race if the reservation weren't already atomic and prior to
        # this call.
        with call_lock:
            calls.append(user_id)
        time.sleep(0.02)
        fd, path = tempfile.mkstemp(prefix="ratatoskr_dump_rl_test_", suffix=".dump")
        os.close(fd)
        with open(path, "wb") as fh:
            fh.write(b"fake-dump")
        return DatabaseDumpFile(path=path, filename="fake.dump")

    n_requests = 8
    with patch(
        "app.api.services.system_maintenance_service.SystemMaintenanceService.build_db_dump_file",
        new=_fake_build,
    ):
        with ThreadPoolExecutor(max_workers=n_requests) as pool:
            futures = [
                pool.submit(client.get, "/v1/system/db-dump", headers=headers)
                for _ in range(n_requests)
            ]
            responses = [future.result() for future in futures]

    status_codes = [response.status_code for response in responses]
    assert status_codes.count(200) == 3, status_codes
    assert status_codes.count(429) == n_requests - 3, status_codes
    assert len(calls) == 3, (
        "pg_dump must run at most 3 times even when far more than 3 requests "
        "race in at once -- the rate limit slot is reserved atomically before "
        "the expensive work starts, not counted from an audit row written "
        "after the fact"
    )


# ---------------------------------------------------------------------------
# db-info endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_info_requires_owner(client: TestClient, db, user_factory):
    user = await user_factory(
        telegram_user_id=_allowed_user_id(), username="db_info_user", is_owner=False
    )
    headers = _headers(user.telegram_user_id, client_id="test")

    forbidden_resp = client.get("/v1/system/db-info", headers=headers)
    assert forbidden_resp.status_code == 403

    async with db.transaction() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == user.telegram_user_id).values(is_owner=True)
        )

    ok_resp = client.get("/v1/system/db-info", headers=headers)
    assert ok_resp.status_code == 200
    data = ok_resp.json().get("data", {})
    assert "file_size_mb" in data
    assert "table_counts" in data


@pytest.mark.asyncio
async def test_db_info_skips_unallowlisted_tables(client: TestClient, db, user_factory):
    owner = await user_factory(
        telegram_user_id=_allowed_user_id(), username="owner_user3", is_owner=True
    )
    owner_headers = _headers(owner.telegram_user_id, client_id="test")

    response = client.get("/v1/system/db-info", headers=owner_headers)

    assert response.status_code == 200
    table_counts = response.json().get("data", {}).get("table_counts", {})
    assert "unexpected_table" not in table_counts
    assert "requests" in table_counts


# ---------------------------------------------------------------------------
# clear-cache endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_cache_requires_owner(client: TestClient, db, user_factory):
    from unittest.mock import AsyncMock

    user = await user_factory(
        telegram_user_id=_allowed_user_id(), username="cache_user", is_owner=False
    )
    headers = _headers(user.telegram_user_id, client_id="test")

    forbidden_resp = client.post("/v1/system/clear-cache", headers=headers)
    assert forbidden_resp.status_code == 403

    async with db.transaction() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == user.telegram_user_id).values(is_owner=True)
        )

    with patch(
        "app.api.services.system_maintenance_service.SystemMaintenanceService.clear_url_cache",
        new=AsyncMock(return_value=0),
    ):
        ok_resp = client.post("/v1/system/clear-cache", headers=headers)
    assert ok_resp.status_code == 200
    assert ok_resp.json().get("data", {}).get("cleared_keys") == 0
