"""Contract-level regression tests for /v1/sync (Sync v2).

Covers ordering, pagination, chunk limits, session TTL, upload whitelist, and
conflict envelope shape. Does NOT duplicate the scenarios already in test_sync.py
(session creation, idempotency, stale-version rejection, monotonicity).

Uses the direct-call pattern (no HTTP TestClient) to avoid the asyncpg/anyio
event-loop conflict. Pattern cloned verbatim from tests/api/test_sync.py.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from pydantic import ValidationError

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
# Helpers  (verbatim layout from test_sync.py)
# ---------------------------------------------------------------------------


def _make_svc(db) -> SyncService:
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
    def __init__(self) -> None:
        self.headers: dict = {}


class _FakeResponse:
    def __init__(self) -> None:
        self.headers: dict = {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def contract_user(user_factory):
    return await user_factory(username="contract_test_user")


# ---------------------------------------------------------------------------
# Scenario 1: items strictly ordered by server_version (ascending)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_sync_items_ordered_by_server_version(db, contract_user, summary_factory):
    """Full-sync items list must be sorted ascending by server_version."""
    for _ in range(4):
        await summary_factory(user=contract_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(contract_user)

    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    page = await full_sync(session_id=session_id, limit=500, user=user_ctx, svc=svc)
    assert page["success"] is True
    items = page["data"]["items"]
    assert len(items) >= 4

    versions = [it["server_version"] for it in items]
    assert versions == sorted(versions), f"items not sorted ascending by server_version: {versions}"


@pytest.mark.asyncio
async def test_delta_sync_items_ordered_by_server_version(db, contract_user, summary_factory):
    """Delta-sync created list must be sorted ascending by server_version."""
    for _ in range(3):
        await summary_factory(user=contract_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(contract_user)

    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    delta = await delta_sync(
        request=_FakeRequest(),
        response=_FakeResponse(),
        session_id=session_id,
        since=0,
        limit=500,
        user=user_ctx,
        svc=svc,
    )
    assert isinstance(delta, dict) and delta["success"] is True
    created = delta["data"]["created"]
    assert len(created) >= 3

    versions = [it["server_version"] for it in created]
    assert versions == sorted(versions), (
        f"delta created items not sorted ascending by server_version: {versions}"
    )


# ---------------------------------------------------------------------------
# Scenario 2: pagination — has_more + next_since correctness across pages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delta_pagination_covers_all_entities_without_duplicates(
    db, contract_user, summary_factory
):
    """Multi-page delta pulls must: union == all entities, no duplicates,
    has_more False only on last page, next_since advances each time."""
    for _ in range(5):
        await summary_factory(user=contract_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(contract_user)

    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    # Key by (entity_type, id) — bare integer IDs are NOT unique across entity types
    # (e.g. a request and a summary can both have id=1).
    all_keys: list[tuple] = []
    since = 0
    prev_since = -1
    page_count = 0
    MAX_PAGES = 50  # safety valve

    while page_count < MAX_PAGES:
        delta = await delta_sync(
            request=_FakeRequest(),
            response=_FakeResponse(),
            session_id=session_id,
            since=since,
            limit=2,
            user=user_ctx,
            svc=svc,
        )
        assert isinstance(delta, dict) and delta["success"] is True
        data = delta["data"]
        page_count += 1

        for it in data["created"] + data["deleted"]:
            all_keys.append((it["entity_type"], it["id"]))

        has_more = data["has_more"]
        next_since = data["next_since"]

        if not has_more:
            break

        # next_since must advance on every non-final page
        assert next_since is not None, "next_since must not be None when has_more=True"
        assert next_since > prev_since, f"next_since did not advance: {next_since} <= {prev_since}"
        prev_since = next_since
        since = next_since

    # No duplicate (entity_type, id) keys across pages
    assert len(all_keys) == len(set(all_keys)), (
        f"duplicate (entity_type, id) keys found across pages: {all_keys}"
    )
    # We seeded at least 5 summaries; union should contain at least that many items
    assert len(all_keys) >= 5


# ---------------------------------------------------------------------------
# Scenario 3a: chunk limit — router rejects limit > 500 via Query validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_sync_rejects_limit_above_500(db, contract_user):
    """The router Query(ge=1, le=500) means limit=600 never reaches the handler.
    Calling full_sync directly bypasses FastAPI validation, so we verify via
    the pydantic SyncSessionRequest that limit is clamped at the request model
    level instead. At the router boundary, FastAPI would return 422.

    NOTE: direct handler calls skip FastAPI Query validation. This test
    documents that limit=600 passed directly is clamped server-side by
    _resolve_limit to max_limit (500) rather than causing an error in the
    service layer — consistent with the spec intent of 'server downsizes'.
    """
    svc = _make_svc(db)
    user_ctx = _user_ctx(contract_user)

    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    # Verify the FastAPI query boundary via pydantic: SyncSessionRequest
    # validates limit in [1, 500].
    with pytest.raises(ValidationError):
        SyncSessionRequest(limit=600)

    # Service-layer: _resolve_limit clamps 600 to max_limit (500) without error.
    resolved = svc._resolve_limit(600)
    assert resolved == svc.cfg.sync.max_limit, (
        f"expected _resolve_limit(600) == {svc.cfg.sync.max_limit}, got {resolved}"
    )


@pytest.mark.asyncio
async def test_full_sync_uses_default_limit_when_none(db, contract_user, summary_factory):
    """Passing limit=None resolves to the session chunk_limit (or config default)."""
    for _ in range(3):
        await summary_factory(user=contract_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(contract_user)

    session_result = await create_sync_session(
        body=SyncSessionRequest(limit=None), user=user_ctx, svc=svc
    )
    session_id = session_result["data"]["session_id"]

    page = await full_sync(session_id=session_id, limit=None, user=user_ctx, svc=svc)
    assert page["success"] is True
    # Pagination limit lives inside data (FullSyncResponseData embeds it there).
    assert page["data"]["pagination"]["limit"] == svc.cfg.sync.default_limit


# ---------------------------------------------------------------------------
# Scenario 4: session TTL — expired session raises SyncSessionExpiredError (410)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_session_raises_410(db, contract_user):
    """Mutating expires_at to the past causes _load_session to raise
    SyncSessionExpiredError with status_code=410."""
    from app.api.exceptions import SyncSessionExpiredError

    svc = _make_svc(db)
    user_ctx = _user_ctx(contract_user)

    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    # Directly mutate the in-memory fallback store's expires_at to the past.
    svc._sync_sessions[session_id]["expires_at"] = "2000-01-01T00:00:00Z"

    with pytest.raises(SyncSessionExpiredError) as exc_info:
        await full_sync(session_id=session_id, limit=10, user=user_ctx, svc=svc)

    exc = exc_info.value
    assert exc.status_code == 410
    from app.api.exceptions import ErrorCode

    assert exc.error_code == ErrorCode.SYNC_SESSION_EXPIRED


# ---------------------------------------------------------------------------
# Scenario 5a: whitelist — unknown fields return INVALID_FIELDS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_unknown_field_returns_invalid_fields(db, contract_user, summary_factory):
    """Payload with an unrecognised field must produce status=invalid,
    error_code=INVALID_FIELDS. Tests actual code behaviour (allowed_fields={'is_read'})."""
    summary = await summary_factory(user=contract_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(contract_user)

    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    payload = SyncApplyRequest(
        session_id=session_id,
        changes=[
            SyncApplyItem(
                entity_type="summary",
                id=summary.id,
                action="update",
                last_seen_version=summary.server_version,
                payload={"some_field": "x"},
            )
        ],
    )
    result = await apply_changes(payload=payload, user=user_ctx, svc=svc)

    assert result["success"] is True
    item = result["data"]["results"][0]
    assert item["status"] == "invalid", f"expected invalid, got: {item['status']}"
    assert item["error_code"] == "INVALID_FIELDS", (
        f"expected INVALID_FIELDS, got: {item['error_code']}"
    )


# ---------------------------------------------------------------------------
# Scenario 5b: whitelist — is_read succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_is_read_field_succeeds(db, contract_user, summary_factory):
    """is_read is the only whitelisted field; it must produce status=applied."""
    summary = await summary_factory(user=contract_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(contract_user)

    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    payload = SyncApplyRequest(
        session_id=session_id,
        changes=[
            SyncApplyItem(
                entity_type="summary",
                id=summary.id,
                action="update",
                last_seen_version=summary.server_version,
                payload={"is_read": True},
            )
        ],
    )
    result = await apply_changes(payload=payload, user=user_ctx, svc=svc)

    assert result["success"] is True
    item = result["data"]["results"][0]
    assert item["status"] == "applied", f"expected applied, got: {item['status']}"


# ---------------------------------------------------------------------------
# Scenario 5c: whitelist — non-summary entity type returns UNSUPPORTED_ENTITY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_non_summary_entity_returns_unsupported_entity(db, contract_user):
    """entity_type='request' (or any non-summary) must return UNSUPPORTED_ENTITY."""
    svc = _make_svc(db)
    user_ctx = _user_ctx(contract_user)

    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    payload = SyncApplyRequest(
        session_id=session_id,
        changes=[
            SyncApplyItem(
                entity_type="request",
                id="99999",
                action="update",
                last_seen_version=0,
                payload={"is_read": True},
            )
        ],
    )
    result = await apply_changes(payload=payload, user=user_ctx, svc=svc)

    assert result["success"] is True
    item = result["data"]["results"][0]
    assert item["status"] == "invalid"
    assert item["error_code"] == "UNSUPPORTED_ENTITY"


# ---------------------------------------------------------------------------
# Scenario 6: conflict envelope shape — conflicts[] non-null + server_snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_conflict_envelope_contains_server_snapshot(db, contract_user, summary_factory):
    """A stale apply must include conflicts[] with server_snapshot so the client
    can resolve the conflict. This is the *contract* angle complementing the
    stale-version test in test_sync.py."""
    summary = await summary_factory(user=contract_user)

    svc = _make_svc(db)
    user_ctx = _user_ctx(contract_user)

    session_result = await create_sync_session(body=None, user=user_ctx, svc=svc)
    session_id = session_result["data"]["session_id"]

    payload = SyncApplyRequest(
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
    result = await apply_changes(payload=payload, user=user_ctx, svc=svc)

    assert result["success"] is True
    data = result["data"]

    # conflicts list must be non-null and non-empty
    conflicts = data.get("conflicts")
    assert conflicts, f"Expected non-empty conflicts list, got: {conflicts!r}"

    conflict_item = conflicts[0]
    # server_snapshot must be present so the client can resolve
    assert conflict_item.get("server_snapshot") is not None, (
        f"server_snapshot missing from conflict item: {conflict_item}"
    )
    # results[0] must also carry server_snapshot
    result_item = data["results"][0]
    assert result_item.get("server_snapshot") is not None, (
        f"server_snapshot missing from results[0]: {result_item}"
    )
    assert result_item["status"] == "conflict"
