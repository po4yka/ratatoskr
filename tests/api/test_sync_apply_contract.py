"""Sync /v1/sync/apply response shape contract.

Locks the JSON shape that ratatoskr-client (and the KMP client behind
[[map-ratatoskr-mobile-api-contract-to-kmp-readiness]]) consume:
session_id / results[] / conflicts[]? / has_more?, with snake_case sync keys
on every nested envelope. Failures here mean a backend change has shifted the
wire format and the client needs to re-validate.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.api.exceptions import SyncSessionExpiredError, SyncSessionForbiddenError
from app.api.models.requests import SyncApplyItem, SyncApplyRequest, SyncSessionRequest
from app.api.models.responses import (
    DeltaSyncResponseData,
    FullSyncResponseData,
    PaginationInfo,
    SyncApplyItemResult,
    SyncApplyResponseData,
    SyncEntityEnvelope,
    SyncSessionData,
    success_response,
)
from app.api.routers.sync import (
    _build_delta_etag,
    apply_changes,
    create_sync_session,
    delta_sync,
    full_sync,
)


def _success_item(entity_type: str, id_: int | str, server_version: int) -> SyncApplyItemResult:
    return SyncApplyItemResult(
        entity_type=entity_type,
        id=id_,
        status="applied",
        server_version=server_version,
    )


def _conflict_item(
    entity_type: str,
    id_: int | str,
    server_version: int,
    server_snapshot: SyncEntityEnvelope,
    error_code: str = "version_mismatch",
) -> SyncApplyItemResult:
    return SyncApplyItemResult(
        entity_type=entity_type,
        id=id_,
        status="conflict",
        server_version=server_version,
        server_snapshot=server_snapshot,
        error_code=error_code,
    )


def test_apply_response_serializes_snake_case_top_level() -> None:
    response = SyncApplyResponseData(
        session_id="sync-session-abc",
        results=[_success_item("summary", 42, 7)],
    )
    payload = response.model_dump(by_alias=True, exclude_none=True)

    # Top-level keys are the public snake_case sync contract.
    assert set(payload.keys()) == {"session_id", "results"}
    assert payload["session_id"] == "sync-session-abc"


def test_apply_response_item_uses_snake_case_keys_and_string_id() -> None:
    response = SyncApplyResponseData(
        session_id="sync-session-abc",
        results=[_success_item("summary", 42, 7)],
    )
    item = response.model_dump(by_alias=True, exclude_none=True)["results"][0]

    assert item == {
        "entity_type": "summary",
        "id": "42",
        "status": "applied",
        "server_version": 7,
    }


def test_apply_response_includes_conflict_with_full_aliases() -> None:
    snapshot = SyncEntityEnvelope(
        entity_type="summary",
        id=43,
        server_version=12,
        updated_at="2026-05-21T00:00:00Z",
        summary={
            "id": 43,
            "request_id": 5,
            "lang": "en",
            "is_read": False,
            "version": 1,
            "json_payload": {},
            "created_at": "2026-05-21T00:00:00Z",
        },
    )
    response = SyncApplyResponseData(
        session_id="sync-session-abc",
        results=[
            _success_item("summary", 42, 7),
            _conflict_item(
                entity_type="summary",
                id_=43,
                server_version=12,
                server_snapshot=snapshot,
            ),
        ],
        conflicts=[
            _conflict_item(
                entity_type="summary",
                id_=43,
                server_version=12,
                server_snapshot=snapshot,
            )
        ],
    )
    payload = response.model_dump(by_alias=True, exclude_none=True)

    assert payload["conflicts"][0] == {
        "entity_type": "summary",
        "id": "43",
        "status": "conflict",
        "server_version": 12,
        "server_snapshot": {
            "entity_type": "summary",
            "id": "43",
            "server_version": 12,
            "updated_at": "2026-05-21T00:00:00Z",
            "summary": {
                "id": 43,
                "request_id": 5,
                "lang": "en",
                "is_read": False,
                "version": 1,
                "json_payload": {},
                "created_at": "2026-05-21T00:00:00Z",
            },
        },
        "error_code": "version_mismatch",
    }


def test_apply_response_has_more_round_trips_as_snake_case() -> None:
    truthy = SyncApplyResponseData(
        session_id="sync-session-abc",
        results=[_success_item("summary", 1, 1)],
        has_more=True,
    )
    payload_truthy = truthy.model_dump(by_alias=True, exclude_none=True)
    assert payload_truthy["has_more"] is True
    assert "hasMore" not in payload_truthy

    # Default (None): omitted under exclude_none — matches the OpenAPI optional.
    omitted = SyncApplyResponseData(
        session_id="sync-session-abc",
        results=[_success_item("summary", 1, 1)],
    )
    assert "has_more" not in omitted.model_dump(by_alias=True, exclude_none=True)


def test_apply_response_envelope_via_success_response_helper() -> None:
    response = SyncApplyResponseData(
        session_id="sync-session-abc",
        results=[_success_item("summary", 42, 7)],
    )
    envelope = success_response(response)

    # Outer envelope shape: success / data / meta. data is the snake_case apply
    # payload — this is what the client actually parses.
    assert envelope["success"] is True
    assert "data" in envelope
    assert envelope["data"]["session_id"] == "sync-session-abc"
    assert envelope["data"]["results"][0]["entity_type"] == "summary"
    assert envelope["data"]["results"][0]["server_version"] == 7
    assert "meta" in envelope


def test_sync_response_envelopes_use_snake_case_wire_keys() -> None:
    item = SyncEntityEnvelope(
        entity_type="summary",
        id=42,
        server_version=7,
        updated_at="2026-05-21T00:00:00Z",
        summary={
            "id": 42,
            "request_id": 5,
            "lang": "en",
            "is_read": False,
            "version": 1,
            "json_payload": {},
            "created_at": "2026-05-21T00:00:00Z",
        },
    )
    session = success_response(
        SyncSessionData(
            session_id="sync-session-abc",
            expires_at="2026-05-21T01:00:00Z",
            default_limit=100,
            max_limit=500,
            last_issued_since=0,
        )
    )
    full = success_response(
        FullSyncResponseData(
            session_id="sync-session-abc",
            has_more=False,
            next_since=7,
            items=[item],
            pagination=PaginationInfo(total=1, limit=100, offset=0, has_more=False),
        )
    )
    delta = success_response(
        DeltaSyncResponseData(
            session_id="sync-session-abc",
            since=0,
            has_more=False,
            next_since=7,
            created=[item],
            updated=[],
            deleted=[],
        )
    )
    apply = success_response(
        SyncApplyResponseData(
            session_id="sync-session-abc",
            results=[_success_item("summary", 42, 7)],
        )
    )

    assert session["data"]["session_id"] == "sync-session-abc"
    assert session["data"]["expires_at"] == "2026-05-21T01:00:00Z"
    assert full["data"]["has_more"] is False
    assert full["data"]["next_since"] == 7
    assert full["data"]["items"][0]["entity_type"] == "summary"
    assert full["data"]["items"][0]["id"] == "42"
    assert delta["data"]["created"][0]["server_version"] == 7
    assert apply["data"]["results"][0]["error_code"] is None

    old_keys = {
        "sessionId",
        "expiresAt",
        "defaultLimit",
        "maxLimit",
        "entityType",
        "serverVersion",
        "updatedAt",
        "hasMore",
        "nextSince",
        "errorCode",
    }
    for envelope in (session, full, delta, apply):
        assert not _contains_any_key(envelope["data"], old_keys)


@pytest.mark.asyncio
async def test_sync_router_handlers_emit_snake_case_wire_keys() -> None:
    item = SyncEntityEnvelope(
        entity_type="summary",
        id=42,
        server_version=7,
        updated_at="2026-05-21T00:00:00Z",
    )
    svc = _FakeSyncService(item)
    user = {"user_id": 123, "client_id": "test-client"}

    session = await create_sync_session(body=SyncSessionRequest(limit=50), user=user, svc=svc)
    full = await full_sync(session_id="sync-session-abc", limit=50, user=user, svc=svc)
    delta = await delta_sync(
        request=cast("Any", SimpleNamespace(headers={})),
        response=cast("Any", SimpleNamespace(headers={})),
        session_id="sync-session-abc",
        since=0,
        limit=50,
        user=user,
        svc=svc,
    )
    apply = await apply_changes(
        payload=SyncApplyRequest(
            session_id="sync-session-abc",
            changes=[
                SyncApplyItem(
                    entity_type="summary",
                    id=42,
                    action="update",
                    last_seen_version=7,
                    payload={"is_read": True},
                )
            ],
        ),
        user=user,
        svc=svc,
    )

    assert isinstance(delta, dict)
    assert session["data"]["session_id"] == "sync-session-abc"
    assert full["data"]["items"][0]["id"] == "42"
    assert full["data"]["items"][0]["server_version"] == 7
    assert delta["data"]["created"][0]["entity_type"] == "summary"
    assert apply["data"]["results"][0]["entity_type"] == "summary"
    assert apply["data"]["results"][0]["id"] == "42"
    for envelope in (session, full, delta, apply):
        assert not _contains_any_key(envelope["data"], {"sessionId", "entityType", "serverVersion"})


@pytest.mark.asyncio
async def test_apply_route_forwards_idempotency_key() -> None:
    item = SyncEntityEnvelope(
        entity_type="summary",
        id=42,
        server_version=7,
        updated_at="2026-05-21T00:00:00Z",
    )
    svc = _FakeSyncService(item)

    response = await apply_changes(
        payload=SyncApplyRequest(
            session_id="sync-session-abc",
            idempotency_key="route-retry-key",
            changes=[
                SyncApplyItem(
                    entity_type="summary",
                    id=42,
                    action="update",
                    last_seen_version=7,
                    payload={"is_read": True},
                )
            ],
        ),
        user={"user_id": 123, "client_id": "test-client"},
        svc=svc,
    )

    assert response["data"]["results"][0]["status"] == "applied"
    assert svc.idempotency_keys == ["route-retry-key"]


@pytest.mark.asyncio
async def test_delta_valid_session_with_matching_etag_returns_304_without_db() -> None:
    item = SyncEntityEnvelope(
        entity_type="summary",
        id=42,
        server_version=7,
        updated_at="2026-05-21T00:00:00Z",
    )
    svc = _FakeSyncService(item)
    user = {"user_id": 123, "client_id": "test-client"}

    first_response = SimpleNamespace(headers={})
    first = await delta_sync(
        request=cast("Any", SimpleNamespace(headers={})),
        response=cast("Any", first_response),
        session_id="sync-session-abc",
        since=0,
        limit=50,
        user=user,
        svc=svc,
    )
    assert isinstance(first, dict)
    etag = first_response.headers["ETag"]
    assert etag == _build_delta_etag("sync-session-abc", 7)
    assert "sync-session-abc" not in etag

    second = await delta_sync(
        request=cast("Any", SimpleNamespace(headers={"if-none-match": etag})),
        response=cast("Any", SimpleNamespace(headers={})),
        session_id="sync-session-abc",
        since=0,
        limit=50,
        user=user,
        svc=svc,
    )

    assert getattr(second, "status_code", None) == 304
    assert second.headers["ETag"] == etag


@pytest.mark.asyncio
async def test_delta_expired_session_with_matching_etag_does_not_return_304_without_db() -> None:
    svc = _RejectingSyncService(SyncSessionExpiredError("sync-session-abc"))
    etag = _build_delta_etag("sync-session-abc", 7)

    with pytest.raises(SyncSessionExpiredError):
        await delta_sync(
            request=cast("Any", SimpleNamespace(headers={"if-none-match": etag})),
            response=cast("Any", SimpleNamespace(headers={})),
            session_id="sync-session-abc",
            since=0,
            limit=50,
            user={"user_id": 123, "client_id": "test-client"},
            svc=svc,
        )

    assert svc.max_version_calls == 0
    assert svc.delta_calls == 0


@pytest.mark.asyncio
async def test_delta_forbidden_session_with_matching_etag_does_not_return_304_without_db() -> None:
    svc = _RejectingSyncService(SyncSessionForbiddenError())
    etag = _build_delta_etag("sync-session-abc", 7)

    with pytest.raises(SyncSessionForbiddenError):
        await delta_sync(
            request=cast("Any", SimpleNamespace(headers={"if-none-match": etag})),
            response=cast("Any", SimpleNamespace(headers={})),
            session_id="sync-session-abc",
            since=0,
            limit=50,
            user={"user_id": 123, "client_id": "other-client"},
            svc=svc,
        )

    assert svc.max_version_calls == 0
    assert svc.delta_calls == 0


def _contains_any_key(value: object, keys: set[str]) -> bool:
    if isinstance(value, dict):
        nested_values = [nested for key, nested in value.items() if key != "pagination"]
        return bool(keys & set(value)) or any(
            _contains_any_key(nested, keys) for nested in nested_values
        )
    if isinstance(value, list):
        return any(_contains_any_key(nested, keys) for nested in value)
    return False


class _FakeSyncService:
    def __init__(self, item: SyncEntityEnvelope) -> None:
        self.item = item
        self.cfg = SimpleNamespace(sync=SimpleNamespace(default_limit=50))
        self.idempotency_keys: list[str | None] = []

    async def start_session(
        self, *, user_id: int, client_id: str | None, limit: int | None
    ) -> SyncSessionData:
        _ = user_id, client_id, limit
        return SyncSessionData(
            session_id="sync-session-abc",
            expires_at="2026-05-21T01:00:00Z",
            default_limit=50,
            max_limit=500,
            last_issued_since=0,
        )

    async def get_full(
        self, *, session_id: str, user_id: int, client_id: str | None, limit: int | None
    ) -> FullSyncResponseData:
        _ = user_id, client_id
        return FullSyncResponseData(
            session_id=session_id,
            has_more=False,
            next_since=7,
            items=[self.item],
            pagination=PaginationInfo(total=1, limit=limit or 50, offset=0, has_more=False),
        )

    async def get_max_server_version(self, user_id: int) -> int:
        _ = user_id
        return 7

    async def validate_session(
        self, session_id: str, user_id: int, client_id: str | None
    ) -> dict[str, object]:
        _ = session_id, user_id, client_id
        return {}

    async def get_delta(
        self,
        *,
        session_id: str,
        user_id: int,
        client_id: str | None,
        since: int,
        limit: int | None,
    ) -> DeltaSyncResponseData:
        _ = user_id, client_id, limit
        return DeltaSyncResponseData(
            session_id=session_id,
            since=since,
            has_more=False,
            next_since=7,
            created=[self.item],
            updated=[],
            deleted=[],
        )

    async def apply_changes(
        self,
        *,
        session_id: str,
        user_id: int,
        client_id: str | None,
        changes: list[object],
        idempotency_key: str | None = None,
    ) -> SyncApplyResponseData:
        _ = user_id, client_id, changes
        self.idempotency_keys.append(idempotency_key)
        return SyncApplyResponseData(
            session_id=session_id,
            results=[_success_item("summary", 42, 7)],
        )


class _RejectingSyncService:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.max_version_calls = 0
        self.delta_calls = 0
        self.cfg = SimpleNamespace(sync=SimpleNamespace(default_limit=50))

    async def validate_session(
        self, session_id: str, user_id: int, client_id: str | None
    ) -> dict[str, object]:
        _ = session_id, user_id, client_id
        raise self.error

    async def get_max_server_version(self, user_id: int) -> int:
        _ = user_id
        self.max_version_calls += 1
        return 7

    async def get_delta(
        self,
        *,
        session_id: str,
        user_id: int,
        client_id: str | None,
        since: int,
        limit: int | None,
    ) -> DeltaSyncResponseData:
        _ = session_id, user_id, client_id, since, limit
        self.delta_calls += 1
        return DeltaSyncResponseData(
            session_id="sync-session-abc",
            since=0,
            has_more=False,
            next_since=7,
            created=[],
            updated=[],
            deleted=[],
        )
