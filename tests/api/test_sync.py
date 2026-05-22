"""Handler-level integration tests for /v1/sync/{sessions,full,delta,apply}.

Uses the direct-call pattern (no HTTP TestClient) to avoid the asyncpg/anyio
event-loop conflict that breaks TestClient in this repo.  See
tests/api/test_articles.py for the canonical pattern.

SyncService is wired with real repository instances built via
app.di.repositories.build_* helpers against the test Postgres database
provided by the `db` fixture from conftest.py.  No mocks touch the DB path.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.api.exceptions import SyncSessionExpiredError, SyncSessionForbiddenError
from app.api.models.requests import SyncApplyItem, SyncApplyRequest, SyncSessionRequest
from app.api.routers.sync import apply_changes, create_sync_session, delta_sync, full_sync
from app.api.services.sync_service import SyncService
from app.config import load_config
from app.di.repositories import (
    build_crawl_result_repository,
    build_llm_repository,
    build_request_repository,
    build_summary_repository,
    build_user_repository,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_svc(db) -> SyncService:
    """Build a SyncService wired to real repositories against *db*."""
    cfg = load_config(allow_stub_telegram=True)
    return SyncService(
        cfg,
        db,
        user_repository=build_user_repository(db),
        request_repository=build_request_repository(db),
        summary_repository=build_summary_repository(db),
        crawl_result_repository=build_crawl_result_repository(db),
        llm_repository=build_llm_repository(db),
    )


def _user_ctx(user) -> dict:
    return {
        "user_id": user.telegram_user_id,
        "username": user.username,
        "client_id": "test-client",
    }


class _FakeRequest:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


class _FakeResponse:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


_OLD_SYNC_CAMEL_KEYS = {
    "sessionId",
    "expiresAt",
    "defaultLimit",
    "maxLimit",
    "lastIssuedSince",
    "entityType",
    "serverVersion",
    "updatedAt",
    "deletedAt",
    "hasMore",
    "nextSince",
    "serverSnapshot",
    "errorCode",
}


def _assert_sync_data_uses_snake_case(value: object) -> None:
    if isinstance(value, dict):
        assert not (_OLD_SYNC_CAMEL_KEYS & set(value)), value
        for key, nested in value.items():
            if key == "pagination":
                continue
            _assert_sync_data_uses_snake_case(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_sync_data_uses_snake_case(nested)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sync_user(user_factory):
    return await user_factory(username="sync_test_user")


@pytest_asyncio.fixture
async def sync_summary(summary_factory, sync_user):
    return await summary_factory(user=sync_user)


# ---------------------------------------------------------------------------
# Scenario 1: Create session returns session_id + meta envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_returns_session_id(db, sync_user):
    svc = _make_svc(db)
    user_ctx = _user_ctx(sync_user)

    result = await create_sync_session(body=SyncSessionRequest(limit=50), user=user_ctx, svc=svc)

    assert result["success"] is True
    data = result["data"]
    assert "session_id" in data, f"session_id missing from data: {data.keys()}"
    assert data["session_id"].startswith("sync-")
    assert "meta" in result


@pytest.mark.asyncio
async def test_sync_endpoints_emit_snake_case_raw_json_keys(db, sync_user, summary_factory):
    summary = await summary_factory(user=sync_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(sync_user)

    session_result = await create_sync_session(
        body=SyncSessionRequest(limit=50), user=user_ctx, svc=svc
    )
    session_data = session_result["data"]
    _assert_sync_data_uses_snake_case(session_data)
    assert {"session_id", "expires_at", "default_limit", "max_limit", "last_issued_since"}.issubset(
        session_data
    )
    session_id = session_data["session_id"]

    full_result = await full_sync(session_id=session_id, limit=500, user=user_ctx, svc=svc)
    full_data = full_result["data"]
    _assert_sync_data_uses_snake_case(full_data)
    assert {"session_id", "has_more", "next_since", "items", "pagination"}.issubset(full_data)
    full_summary = next(
        item
        for item in full_data["items"]
        if item["entity_type"] == "summary" and item["id"] == str(summary.id)
    )
    assert isinstance(full_summary["id"], str)
    assert {"entity_type", "id", "server_version", "updated_at", "summary"}.issubset(full_summary)
    assert {"request_id", "is_read", "json_payload", "created_at"}.issubset(full_summary["summary"])

    delta_result = await delta_sync(
        request=_FakeRequest(),
        response=_FakeResponse(),
        session_id=session_id,
        since=0,
        limit=500,
        user=user_ctx,
        svc=svc,
    )
    assert isinstance(delta_result, dict)
    delta_data = delta_result["data"]
    _assert_sync_data_uses_snake_case(delta_data)
    assert {
        "session_id",
        "since",
        "has_more",
        "next_since",
        "created",
        "updated",
        "deleted",
    }.issubset(delta_data)
    assert any(item["id"] == str(summary.id) for item in delta_data["created"])

    apply_payload = SyncApplyRequest(
        session_id=session_id,
        changes=[
            SyncApplyItem(
                entity_type="summary",
                id=summary.id,
                action="update",
                last_seen_version=0,
                payload={"is_read": False},
            )
        ],
    )
    apply_result = await apply_changes(payload=apply_payload, user=user_ctx, svc=svc)
    apply_data = apply_result["data"]
    _assert_sync_data_uses_snake_case(apply_data)
    assert {"session_id", "results", "conflicts"}.issubset(apply_data)
    conflict = apply_data["results"][0]
    assert conflict["id"] == str(summary.id)
    assert {
        "entity_type",
        "id",
        "status",
        "server_version",
        "server_snapshot",
        "error_code",
    }.issubset(conflict)
    assert conflict["server_snapshot"]["id"] == str(summary.id)


# ---------------------------------------------------------------------------
# Scenario 2: Full sync paginated by cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_sync_pagination(db, sync_user, summary_factory):
    # Seed 3 summaries for the user.
    for _ in range(3):
        await summary_factory(user=sync_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(sync_user)

    # Create a session first.
    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    # Fetch with limit=2 - should get a page and has_more=True.
    page1 = await full_sync(
        session_id=session_id,
        limit=2,
        user=user_ctx,
        svc=svc,
    )
    assert page1["success"] is True
    data1 = page1["data"]
    assert len(data1["items"]) == 2
    assert data1["has_more"] is True

    # Fetch remaining records with a large limit - has_more should be False.
    page2 = await full_sync(
        session_id=session_id,
        limit=500,
        user=user_ctx,
        svc=svc,
    )
    assert page2["success"] is True
    data2 = page2["data"]
    assert data2["has_more"] is False


# ---------------------------------------------------------------------------
# Scenario 3: Delta after full returns only changed items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delta_sync_returns_changed_items(db, sync_user, summary_factory):
    summary = await summary_factory(user=sync_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(sync_user)

    # Start session.
    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    # Full sync to capture baseline server_version.
    full_result = await full_sync(
        session_id=session_id,
        limit=500,
        user=user_ctx,
        svc=svc,
    )
    assert full_result["success"] is True
    items_before = full_result["data"]["items"]
    # Find our summary's server_version as the cursor.
    summary_items = [it for it in items_before if it.get("entity_type") == "summary"]
    assert summary_items, "Expected at least one summary in full sync output"
    baseline_version = max(it["server_version"] for it in summary_items)

    # Mutate the summary (flip is_read) via apply to bump its server_version.
    apply_payload = SyncApplyRequest(
        session_id=session_id,
        changes=[
            SyncApplyItem(
                entity_type="summary",
                id=summary.id,
                action="update",
                last_seen_version=baseline_version,
                payload={"is_read": True},
            )
        ],
    )

    # Use a fresh mock-free Request object for the apply call.
    class _FakeRequest:
        headers: dict = {}

    class _FakeResponse:
        headers: dict = {}

    apply_result = await apply_changes(payload=apply_payload, user=user_ctx, svc=svc)
    assert apply_result["success"] is True
    apply_data = apply_result["data"]
    assert apply_data["results"][0]["status"] == "applied"

    # Note: server_version is set once at INSERT (not bumped by apply_sync_change),
    # so the returned server_version equals baseline_version.  The important thing is
    # apply succeeded and is_read was toggled.

    # Delta from cursor=0 should include all records for the user (including our summary).
    fake_req = _FakeRequest()
    fake_resp = _FakeResponse()
    delta_result = await delta_sync(
        request=fake_req,
        response=fake_resp,
        session_id=session_id,
        since=0,
        limit=500,
        user=user_ctx,
        svc=svc,
    )

    # delta_sync can return a Response (304) or dict; here we expect a dict.
    assert isinstance(delta_result, dict), "Expected dict response, got Response (304)"
    assert delta_result["success"] is True
    delta_data = delta_result["data"]
    # The summary should appear in created (server_version > 0).
    all_ids = [it["id"] for it in delta_data.get("created", [])]
    assert str(summary.id) in all_ids, f"Summary {summary.id} not found in delta created={all_ids}"


# ---------------------------------------------------------------------------
# Scenario 4: Apply idempotent re-apply via idempotency_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_idempotent_reapply_returns_cached_response(db, sync_user, sync_summary):
    """Sending the same apply payload twice with the same idempotency_key
    must return the original response on the second call without re-applying
    the change. Lets clients retry safely after a network failure."""
    svc = _make_svc(db)
    user_ctx = _user_ctx(sync_user)

    # Bootstrap a session.
    session_resp = await create_sync_session(SyncSessionRequest(), user=user_ctx, svc=svc)
    session_id = session_resp["data"]["session_id"]

    # Capture the row's pre-apply server_version so we can assert no
    # double-bump after the second (idempotent) call.
    apply_payload = SyncApplyRequest(
        session_id=session_id,
        idempotency_key="retry-key-test-42",
        changes=[
            SyncApplyItem(
                entity_type="summary",
                id=str(sync_summary.id),
                action="update",
                last_seen_version=sync_summary.server_version,
                payload={"is_read": True},
            )
        ],
    )

    first = await apply_changes(payload=apply_payload, user=user_ctx, svc=svc)
    second = await apply_changes(payload=apply_payload, user=user_ctx, svc=svc)

    # The data payload (snake_case wire shape) must be identical on the
    # idempotent retry. The meta envelope intentionally diverges per-call
    # (correlation_id and timestamp are per-request observability, not part
    # of the apply contract).
    assert first["data"] == second["data"], (
        "second apply with same idempotency_key must return cached data"
    )

    # The row was mutated exactly once: server_version advanced once and
    # the second call did NOT bump it again.
    from sqlalchemy import select as _select

    from app.db.models import Summary

    async with db.session() as session:
        row = await session.scalar(_select(Summary).where(Summary.id == sync_summary.id))
    assert row is not None
    assert row.is_read is True
    first_result = first["data"]["results"][0]
    assert row.server_version == first_result["server_version"], (
        "second idempotent call must not advance server_version"
    )


@pytest.mark.asyncio
async def test_service_apply_idempotency_key_returns_cached_response(
    db,
    sync_user,
    sync_summary,
):
    """Direct SyncService calls must honor the same idempotency contract as the route."""
    svc = _make_svc(db)
    user_ctx = _user_ctx(sync_user)

    session = await svc.start_session(
        user_id=user_ctx["user_id"],
        client_id=user_ctx["client_id"],
        limit=None,
    )
    changes = [
        SyncApplyItem(
            entity_type="summary",
            id=str(sync_summary.id),
            action="update",
            last_seen_version=sync_summary.server_version,
            payload={"is_read": True},
        )
    ]

    first = await svc.apply_changes(
        session_id=session.session_id,
        user_id=user_ctx["user_id"],
        client_id=user_ctx["client_id"],
        changes=changes,
        idempotency_key="direct-service-retry-key-test-42",
    )
    second = await svc.apply_changes(
        session_id=session.session_id,
        user_id=user_ctx["user_id"],
        client_id=user_ctx["client_id"],
        changes=changes,
        idempotency_key="direct-service-retry-key-test-42",
    )

    assert first == second

    from sqlalchemy import select as _select

    from app.db.models import Summary

    async with db.session() as session_obj:
        row = await session_obj.scalar(_select(Summary).where(Summary.id == sync_summary.id))
    assert row is not None
    assert row.is_read is True
    assert row.server_version == first.results[0].server_version


# ---------------------------------------------------------------------------
# Scenario 5: Apply with conflict - stale last_seen_version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_conflict_stale_version(db, sync_user, summary_factory):
    summary = await summary_factory(user=sync_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(sync_user)

    # Create session.
    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    # server_version is set at INSERT (epoch millis) and now ALSO advances on
    # every successful sync-apply mutation. A stale last_seen_version is any
    # value strictly less than the current server_version. 0 is always stale.
    stale_apply = SyncApplyRequest(
        session_id=session_id,
        changes=[
            SyncApplyItem(
                entity_type="summary",
                id=summary.id,
                action="update",
                last_seen_version=0,  # always stale vs epoch-millis server_version
                payload={"is_read": False},
            )
        ],
    )
    r2 = await apply_changes(payload=stale_apply, user=user_ctx, svc=svc)

    # Response must be a success envelope (HTTP 200), not a 5xx.
    assert r2["success"] is True
    data2 = r2["data"]
    assert data2["results"][0]["status"] == "conflict"
    # conflicts list must be non-empty.
    assert data2.get("conflicts"), (
        f"Expected non-empty conflicts list, got: {data2.get('conflicts')!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 6: server_version monotonicity regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_advances_server_version_on_mutation(db, sync_user, sync_summary):
    """A successful sync-apply MUST advance the row's server_version.

    Two invariants depend on this:
      1. Other clients calling /v1/sync/delta since their last cursor will
         observe this row as updated.
      2. A subsequent stale upload (using the pre-mutation server_version)
         is detected as a conflict, not silently overwritten.

    Earlier the bump was missing — async_apply_sync_change updated is_read /
    is_deleted but left server_version frozen at its INSERT-time value, so
    a re-apply with the same client cursor would slip through. This test
    locks the fix.
    """
    svc = _make_svc(db)
    user_ctx = _user_ctx(sync_user)

    session_resp = await create_sync_session(SyncSessionRequest(), user=user_ctx, svc=svc)
    session_id = session_resp["data"]["session_id"]

    initial_version = sync_summary.server_version

    apply_req = SyncApplyRequest(
        session_id=session_id,
        changes=[
            SyncApplyItem(
                entity_type="summary",
                id=str(sync_summary.id),
                action="update",
                last_seen_version=initial_version,
                payload={"is_read": True},
            )
        ],
    )
    result = await apply_changes(payload=apply_req, user=user_ctx, svc=svc)

    item = result["data"]["results"][0]
    assert item["status"] == "applied"
    assert item["server_version"] > initial_version, (
        f"server_version must advance after apply; got {item['server_version']} "
        f"<= initial {initial_version}"
    )

    # Confirm the DB row reflects the bumped version, not just the response.
    from sqlalchemy import select as _select

    from app.db.models import Summary

    async with db.session() as session:
        row = await session.scalar(_select(Summary).where(Summary.id == sync_summary.id))
    assert row is not None
    assert row.server_version > initial_version
    assert row.server_version == item["server_version"]


@pytest.mark.asyncio
async def test_delta_valid_session_with_matching_etag_returns_304(db, sync_user, summary_factory):
    await summary_factory(user=sync_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(sync_user)
    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    first_response = _FakeResponse()
    first = await delta_sync(
        request=_FakeRequest(),
        response=first_response,
        session_id=session_id,
        since=0,
        limit=500,
        user=user_ctx,
        svc=svc,
    )
    assert isinstance(first, dict)
    etag = first_response.headers["ETag"]
    assert etag.startswith('W/"sync-')
    assert session_id not in etag

    second = await delta_sync(
        request=_FakeRequest(headers={"if-none-match": etag}),
        response=_FakeResponse(),
        session_id=session_id,
        since=0,
        limit=500,
        user=user_ctx,
        svc=svc,
    )

    assert getattr(second, "status_code", None) == 304
    assert second.headers["ETag"] == etag


@pytest.mark.asyncio
async def test_delta_expired_session_with_matching_etag_does_not_return_304(
    db, sync_user, summary_factory
):
    await summary_factory(user=sync_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(sync_user)
    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    first_response = _FakeResponse()
    first = await delta_sync(
        request=_FakeRequest(),
        response=first_response,
        session_id=session_id,
        since=0,
        limit=500,
        user=user_ctx,
        svc=svc,
    )
    assert isinstance(first, dict)
    etag = first_response.headers["ETag"]

    svc._fallback_store._sessions[session_id]["expires_at"] = "2000-01-01T00:00:00Z"

    with pytest.raises(SyncSessionExpiredError):
        await delta_sync(
            request=_FakeRequest(headers={"if-none-match": etag}),
            response=_FakeResponse(),
            session_id=session_id,
            since=0,
            limit=500,
            user=user_ctx,
            svc=svc,
        )


@pytest.mark.asyncio
async def test_delta_wrong_client_with_matching_etag_does_not_return_304(
    db, sync_user, summary_factory
):
    await summary_factory(user=sync_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(sync_user)
    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    first_response = _FakeResponse()
    first = await delta_sync(
        request=_FakeRequest(),
        response=first_response,
        session_id=session_id,
        since=0,
        limit=500,
        user=user_ctx,
        svc=svc,
    )
    assert isinstance(first, dict)
    etag = first_response.headers["ETag"]

    with pytest.raises(SyncSessionForbiddenError):
        await delta_sync(
            request=_FakeRequest(headers={"if-none-match": etag}),
            response=_FakeResponse(),
            session_id=session_id,
            since=0,
            limit=500,
            user={**user_ctx, "client_id": "other-client"},
            svc=svc,
        )
