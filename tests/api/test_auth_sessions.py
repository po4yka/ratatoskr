import asyncio
import hashlib
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select as sa_select

from app.api.dependencies.database import get_auth_repository
from app.api.exceptions import TokenInvalidError, TokenRevokedError
from app.api.models.auth import RefreshTokenRequest
from app.api.routers.auth.endpoints_sessions import refresh_access_token
from app.api.routers.auth.tokens import create_refresh_token
from app.core.time_utils import UTC
from app.db.models import RefreshToken as RefreshTokenModel
from app.observability import metrics


@pytest_asyncio.fixture
async def auth_user(db, user_factory):
    return await user_factory(telegram_user_id=123456789, username="test_auth")


@pytest.mark.asyncio
async def test_create_refresh_token_persists(db, auth_user):
    token, session_id = await create_refresh_token(
        user_id=auth_user.telegram_user_id,
        client_id="test-client",
        device_info="TestDevice",
        ip_address="127.0.0.1",
    )

    assert token is not None
    assert session_id is not None

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    async with db.session() as session:
        count = await session.scalar(sa_select(func.count()).select_from(RefreshTokenModel))
        record = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == token_hash)
        )

    assert count == 1
    assert record is not None
    assert record.user_id == auth_user.telegram_user_id
    assert record.client_id == "test-client"
    assert record.device_info == "TestDevice"
    assert record.ip_address == "127.0.0.1"
    assert not record.is_revoked


@pytest.mark.asyncio
async def test_logout_revokes_token(db, auth_user):
    from app.api.dependencies.database import get_auth_repository
    from app.api.models.auth import RefreshTokenRequest
    from app.api.routers.auth.endpoints_sessions import logout

    token, _ = await create_refresh_token(auth_user.telegram_user_id, "mobile-app")

    http_request = MagicMock()
    http_request.cookies = {}
    response = MagicMock()
    body = RefreshTokenRequest(refresh_token=token)
    current_user = {"user_id": auth_user.telegram_user_id}
    auth_repo = get_auth_repository()

    result = await logout(
        http_request=http_request,
        response=response,
        request=body,
        current_user=current_user,
        auth_repo=auth_repo,
    )

    assert "Logged out" in result["data"]["message"]

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    async with db.session() as session:
        record = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == token_hash)
        )
    assert record is not None
    assert record.is_revoked is True


@pytest.mark.asyncio
async def test_list_sessions(db, auth_user, user_factory):
    from app.api.dependencies.database import get_auth_repository
    from app.api.routers.auth.endpoints_sessions import list_sessions

    # 1. Active session
    await create_refresh_token(auth_user.telegram_user_id, "client-1", device_info="Device 1")

    # 2. Revoked session
    _, _ = await create_refresh_token(
        auth_user.telegram_user_id, "client-2", device_info="Device 2"
    )
    async with db.transaction() as session:
        r2 = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.client_id == "client-2")
        )
        r2.is_revoked = True
        await session.flush()

    # 3. Expired session
    await create_refresh_token(auth_user.telegram_user_id, "client-3", device_info="Device 3")
    async with db.transaction() as session:
        r3 = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.client_id == "client-3")
        )
        r3.expires_at = datetime.now(UTC) - timedelta(days=1)
        await session.flush()

    # 4. Another user's session (should not appear in auth_user's list)
    other = await user_factory(telegram_user_id=67890)
    await create_refresh_token(other.telegram_user_id, "other-client")

    current_user = {"user_id": auth_user.telegram_user_id}
    auth_repo = get_auth_repository()

    result = await list_sessions(current_user=current_user, auth_repo=auth_repo)

    sessions = result["data"]["sessions"]
    # Should only see the single active, non-expired session (client-1)
    assert len(sessions) == 1
    assert sessions[0]["clientId"] == "client-1"
    assert sessions[0]["deviceInfo"] == "Device 1"


# ----- Refresh-token rotation regression tests -------------------------------
#
# All tests above and below use the modern db / user_factory fixtures from
# tests/api/conftest.py and call endpoint functions directly to avoid the
# TestClient / asyncpg event-loop conflict.
#
# Test contract: POST /v1/auth/refresh MUST issue a new refresh token AND
# revoke the previous one. Without this, an attacker who steals a single
# refresh token could keep refreshing indefinitely. A revoked token replayed
# against /refresh MUST trigger reuse detection and revoke ALL of that user's
# refresh tokens (defense in depth — assume the original was stolen).


def _mock_request_response() -> tuple[MagicMock, MagicMock]:
    """Build the minimal Request/Response stubs the refresh handler needs.

    The handler reads `request.cookies.get(...)` (we send the token in the
    request body, so cookies stay empty) and calls `clear_refresh_cookie` /
    `set_refresh_cookie` on the response — MagicMock absorbs both.
    """
    request = MagicMock()
    request.cookies = {}
    return request, MagicMock()


def _refresh_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _token_family_decision_metric_value(decision: str) -> float:
    if not metrics.PROMETHEUS_AVAILABLE:
        return 0.0
    import re

    exported = metrics.get_metrics().decode("utf-8")
    match = re.search(
        rf'^ratatoskr_token_family_decisions_total{{decision="{decision}"}} ([0-9.]+)$',
        exported,
        re.MULTILINE,
    )
    return float(match.group(1)) if match else 0.0


async def _refresh_once(token: str):
    request, response = _mock_request_response()
    payload = RefreshTokenRequest(refresh_token=token)
    return await refresh_access_token(request, response, payload, auth_repo=get_auth_repository())


async def _family_rows(db, family_id: str) -> list[RefreshTokenModel]:
    async with db.session() as session:
        rows = (
            await session.execute(
                sa_select(RefreshTokenModel)
                .where(RefreshTokenModel.family_id == family_id)
                .order_by(RefreshTokenModel.id)
            )
        ).scalars()
        return list(rows.all())


async def _token_row(db, token: str) -> RefreshTokenModel:
    token_hash = _refresh_hash(token)
    async with db.session() as session:
        row = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == token_hash)
        )
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_refresh_rotates_refresh_token_and_revokes_previous(db, user_factory):
    user = await user_factory(telegram_user_id=987654321, username="rotator")

    old_token, _ = await create_refresh_token(
        user_id=user.telegram_user_id,
        client_id="mobile-app",
    )
    old_hash = hashlib.sha256(old_token.encode()).hexdigest()

    request, response = _mock_request_response()
    payload = RefreshTokenRequest(refresh_token=old_token, client_id="mobile-app")
    auth_repo = get_auth_repository()

    result = await refresh_access_token(request, response, payload, auth_repo=auth_repo)
    new_token = result["data"]["tokens"]["refreshToken"]

    # Rotation: returned token differs from the one we sent in.
    assert new_token != old_token

    # Revocation: old hash row flipped to revoked; new hash row exists and live.
    new_hash = hashlib.sha256(new_token.encode()).hexdigest()
    async with db.session() as session:
        old_row = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == old_hash)
        )
        new_row = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == new_hash)
        )

    assert old_row is not None
    assert old_row.is_revoked is True, "previous refresh token must be revoked"
    assert new_row is not None
    assert new_row.is_revoked is False, "new refresh token row must be live"


@pytest.mark.asyncio
async def test_refresh_rotate_chain_keeps_one_family_and_active_leaf(db, user_factory):
    user = await user_factory(telegram_user_id=987654329, username="rotation-chain")

    tokens: list[str] = []
    root_token, _ = await create_refresh_token(
        user_id=user.telegram_user_id,
        client_id="mobile-app",
    )
    tokens.append(root_token)

    current = root_token
    for _ in range(3):
        result = await _refresh_once(current)
        current = result["data"]["tokens"]["refreshToken"]
        tokens.append(current)

    token_hashes = [_refresh_hash(token) for token in tokens]
    async with db.session() as session:
        rows = list(
            (
                await session.execute(
                    sa_select(RefreshTokenModel)
                    .where(RefreshTokenModel.token_hash.in_(token_hashes))
                    .order_by(RefreshTokenModel.id)
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 4
    assert {row.family_id for row in rows} == {rows[0].family_id}
    assert [row.parent_token_hash for row in rows] == [None, *token_hashes[:3]]
    assert [row.is_revoked for row in rows] == [True, True, True, False]


@pytest.mark.asyncio
async def test_refresh_with_revoked_token_revokes_only_its_family_not_other_families(
    db, user_factory
):
    """Token-family policy: replay of a retired token revokes ONLY its own
    family, not every active session the user owns. Sessions on other devices
    (different family_id) must survive.
    """
    user = await user_factory(telegram_user_id=987654322, username="replay-victim")

    revoked_token, _ = await create_refresh_token(
        user_id=user.telegram_user_id,
        client_id="mobile-app",
    )
    other_token, _ = await create_refresh_token(
        user_id=user.telegram_user_id,
        client_id="desktop-app",
    )

    # Pre-revoke one token to simulate a stale/stolen refresh that an attacker
    # later replays. The other token represents the user's still-live session
    # on a different device — its family_id must be different.
    revoked_hash = hashlib.sha256(revoked_token.encode()).hexdigest()
    async with db.transaction() as session:
        row = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == revoked_hash)
        )
        assert row is not None
        row.is_revoked = True
        await session.flush()

    request, response = _mock_request_response()
    payload = RefreshTokenRequest(refresh_token=revoked_token, client_id="mobile-app")
    auth_repo = get_auth_repository()

    with pytest.raises(TokenRevokedError):
        await refresh_access_token(request, response, payload, auth_repo=auth_repo)

    # Family-scoped revocation: the OTHER family (different device) survives.
    other_hash = hashlib.sha256(other_token.encode()).hexdigest()
    async with db.session() as session:
        other_row = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == other_hash)
        )
        revoked_row = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == revoked_hash)
        )
    assert other_row is not None
    assert other_row.is_revoked is False, (
        "family-scoped revoke must NOT touch tokens in other families"
    )
    assert revoked_row is not None
    assert revoked_row.is_revoked is True
    assert other_row.family_id != revoked_row.family_id, (
        "preconditions: tokens from separate create_refresh_token calls must "
        "live in distinct families"
    )


# ----- Token-family rotation regression tests --------------------------------


@pytest.mark.asyncio
async def test_create_refresh_token_persists_with_fresh_family_id(db, auth_user):
    """First-login token gets a fresh family_id (no parent). The id should be
    UUID4-shaped (36 chars with dashes).
    """
    token, _ = await create_refresh_token(
        user_id=auth_user.telegram_user_id,
        client_id="mobile-app",
    )

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    async with db.session() as session:
        row = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == token_hash)
        )

    assert row is not None
    assert row.family_id is not None
    # UUID4: 36 chars with 4 dashes
    assert len(row.family_id) == 36
    assert row.family_id.count("-") == 4
    assert row.parent_token_hash is None  # root of family


@pytest.mark.asyncio
async def test_refresh_persists_family_id_and_parent_token_hash_on_rotation(db, user_factory):
    """Rotation inherits family_id from parent and records parent_token_hash."""
    user = await user_factory(telegram_user_id=987654323, username="chain-walker")

    old_token, _ = await create_refresh_token(
        user_id=user.telegram_user_id,
        client_id="mobile-app",
    )
    old_hash = hashlib.sha256(old_token.encode()).hexdigest()

    async with db.session() as session:
        old_row = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == old_hash)
        )
    assert old_row is not None
    original_family_id = old_row.family_id

    request, response = _mock_request_response()
    payload = RefreshTokenRequest(refresh_token=old_token, client_id="mobile-app")
    auth_repo = get_auth_repository()

    result = await refresh_access_token(request, response, payload, auth_repo=auth_repo)
    new_token = result["data"]["tokens"]["refreshToken"]
    new_hash = hashlib.sha256(new_token.encode()).hexdigest()

    async with db.session() as session:
        new_row = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == new_hash)
        )

    assert new_row is not None
    assert new_row.family_id == original_family_id, "rotation must inherit the parent's family_id"
    assert new_row.parent_token_hash == old_hash, (
        "rotation must chain parent_token_hash to the predecessor"
    )


@pytest.mark.asyncio
async def test_refresh_family_revoke_writes_one_audit_log_row(db, user_factory):
    """REVOKE_FAMILY path writes exactly one AuditLog row with the
    family_id + presented_token_hash_prefix in the payload.
    """
    from app.db.models import AuditLog as AuditLogModel

    user = await user_factory(telegram_user_id=987654324, username="audit-victim")

    revoked_token, _ = await create_refresh_token(
        user_id=user.telegram_user_id,
        client_id="mobile-app",
    )
    revoked_hash = hashlib.sha256(revoked_token.encode()).hexdigest()
    async with db.transaction() as session:
        row = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == revoked_hash)
        )
        assert row is not None
        row.is_revoked = True
        await session.flush()
        family_id = row.family_id

    request, response = _mock_request_response()
    payload = RefreshTokenRequest(refresh_token=revoked_token, client_id="mobile-app")
    auth_repo = get_auth_repository()

    with pytest.raises(TokenRevokedError):
        await refresh_access_token(request, response, payload, auth_repo=auth_repo)

    async with db.session() as session:
        audit_rows = list(
            (
                await session.execute(
                    sa_select(AuditLogModel).where(AuditLogModel.event == "refresh_family_revoked")
                )
            )
            .scalars()
            .all()
        )

    assert len(audit_rows) == 1, "expected exactly one refresh_family_revoked audit row"
    payload_json = audit_rows[0].details_json
    assert payload_json is not None
    assert payload_json.get("family_id") == family_id
    assert payload_json.get("user_id") == user.telegram_user_id
    assert payload_json.get("presented_token_hash_prefix") == revoked_hash[:8]


@pytest.mark.asyncio
async def test_retired_root_replay_revokes_entire_token_family_and_audits(db, user_factory):
    from app.db.models import AuditLog as AuditLogModel

    user = await user_factory(telegram_user_id=987654330, username="family-cascade")

    tokens: list[str] = []
    root_token, _ = await create_refresh_token(
        user_id=user.telegram_user_id,
        client_id="mobile-app",
    )
    tokens.append(root_token)
    current = root_token
    for _ in range(3):
        result = await _refresh_once(current)
        current = result["data"]["tokens"]["refreshToken"]
        tokens.append(current)

    family_id = (await _token_row(db, root_token)).family_id

    revocation_decisions_before = _token_family_decision_metric_value("revoke_family")

    with pytest.raises(TokenRevokedError):
        await _refresh_once(root_token)

    if metrics.PROMETHEUS_AVAILABLE:
        assert (
            _token_family_decision_metric_value("revoke_family") == revocation_decisions_before + 1
        )

    rows = await _family_rows(db, family_id)
    assert len(rows) == 4
    assert all(row.is_revoked for row in rows), "retired-token replay must revoke all siblings"

    async with db.session() as session:
        audit_rows = list(
            (
                await session.execute(
                    sa_select(AuditLogModel).where(AuditLogModel.event == "refresh_family_revoked")
                )
            )
            .scalars()
            .all()
        )

    assert len(audit_rows) == 1
    audit_payload = audit_rows[0].details_json
    assert audit_payload is not None
    assert audit_payload.get("family_id") == family_id
    assert audit_payload.get("reason") == "retired_token_replay"
    assert audit_payload.get("revoked_count") == 1


@pytest.mark.asyncio
async def test_refresh_expired_token_rejects_without_cascading(db, user_factory):
    """REJECT path: expired (not revoked) token rejects without revoking
    sibling family rows or any other family.
    """
    from app.api.exceptions import TokenExpiredError, TokenInvalidError

    user = await user_factory(telegram_user_id=987654325, username="expired-victim")

    # Token that we'll expire post-creation.
    expired_token, _ = await create_refresh_token(
        user_id=user.telegram_user_id,
        client_id="mobile-app",
    )
    other_token, _ = await create_refresh_token(
        user_id=user.telegram_user_id,
        client_id="desktop-app",
    )

    expired_hash = hashlib.sha256(expired_token.encode()).hexdigest()
    async with db.transaction() as session:
        row = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == expired_hash)
        )
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(days=1)
        await session.flush()

    request, response = _mock_request_response()
    payload = RefreshTokenRequest(refresh_token=expired_token, client_id="mobile-app")
    auth_repo = get_auth_repository()

    # JWT-level expiry triggers TokenExpiredError before our handler logic;
    # row-level expiry past JWT exp is the same outcome. Either way we want
    # NO cascade.
    with pytest.raises((TokenExpiredError, TokenInvalidError)):
        await refresh_access_token(request, response, payload, auth_repo=auth_repo)

    other_hash = hashlib.sha256(other_token.encode()).hexdigest()
    async with db.session() as session:
        other_row = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == other_hash)
        )
    assert other_row is not None
    assert other_row.is_revoked is False, "expired-token reject must NOT cascade"


@pytest.mark.asyncio
async def test_expired_active_leaf_rejects_without_revoking_family_siblings(db, user_factory):
    user = await user_factory(telegram_user_id=987654331, username="expired-leaf")

    root_token, _ = await create_refresh_token(
        user_id=user.telegram_user_id,
        client_id="mobile-app",
    )
    child_result = await _refresh_once(root_token)
    child_token = child_result["data"]["tokens"]["refreshToken"]
    child_hash = _refresh_hash(child_token)
    family_id = (await _token_row(db, child_token)).family_id

    async with db.transaction() as session:
        child = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == child_hash)
        )
        assert child is not None
        child.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.flush()

    with pytest.raises(TokenInvalidError):
        await _refresh_once(child_token)

    rows = await _family_rows(db, family_id)
    assert len(rows) == 2
    root, child = rows
    assert root.is_revoked is True
    assert child.is_revoked is False, "expired active leaf should be rejected, not revoked"


@pytest.mark.asyncio
async def test_concurrent_refresh_same_token_does_not_revoke_family(db, user_factory):
    from app.db.models import AuditLog as AuditLogModel

    user = await user_factory(telegram_user_id=987654332, username="concurrent-refresh")

    root_token, _ = await create_refresh_token(
        user_id=user.telegram_user_id,
        client_id="mobile-app",
    )
    family_id = (await _token_row(db, root_token)).family_id

    results = await asyncio.gather(
        _refresh_once(root_token),
        _refresh_once(root_token),
        return_exceptions=True,
    )

    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, Exception)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], TokenInvalidError)

    rows = await _family_rows(db, family_id)
    assert len(rows) == 2
    assert sum(1 for row in rows if not row.is_revoked) == 1, (
        "concurrent same-token refresh should leave the new leaf active"
    )

    async with db.session() as session:
        audit_count = await session.scalar(
            sa_select(func.count())
            .select_from(AuditLogModel)
            .where(AuditLogModel.event == "refresh_family_revoked")
        )
    assert audit_count == 0, "concurrent same-token refresh must not be treated as replay theft"


# ----- POST /v1/auth/logout-all ----------------------------------------------


@pytest.mark.asyncio
async def test_logout_all_revokes_every_active_family_for_user(db, user_factory):
    """POST /v1/auth/logout-all revokes every active family for the current
    user, leaving other users' tokens untouched.
    """
    from app.api.routers.auth.endpoints_sessions import logout_all

    user = await user_factory(telegram_user_id=987654326, username="logout-all-user")
    other_user = await user_factory(telegram_user_id=987654327, username="bystander")

    # Three families for the target user.
    tok1, _ = await create_refresh_token(user.telegram_user_id, "mobile-app")
    tok2, _ = await create_refresh_token(user.telegram_user_id, "desktop-app")
    tok3, _ = await create_refresh_token(user.telegram_user_id, "web-app")
    # Other user's token must NOT be affected.
    other_tok, _ = await create_refresh_token(other_user.telegram_user_id, "mobile-app")

    response = MagicMock()
    current_user = {"user_id": user.telegram_user_id}
    auth_repo = get_auth_repository()

    result = await logout_all(response=response, current_user=current_user, auth_repo=auth_repo)
    assert result["data"]["revokedFamilies"] == 3

    for tok in (tok1, tok2, tok3):
        tok_hash = hashlib.sha256(tok.encode()).hexdigest()
        async with db.session() as session:
            row = await session.scalar(
                sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == tok_hash)
            )
        assert row is not None
        assert row.is_revoked is True, f"logout-all must revoke {tok_hash[:8]}"

    other_hash = hashlib.sha256(other_tok.encode()).hexdigest()
    async with db.session() as session:
        other_row = await session.scalar(
            sa_select(RefreshTokenModel).where(RefreshTokenModel.token_hash == other_hash)
        )
    assert other_row is not None
    assert other_row.is_revoked is False, "logout-all must NOT revoke other users' tokens"


@pytest.mark.asyncio
async def test_logout_all_writes_one_audit_log_per_revoked_family(db, user_factory):
    """logout-all writes one AuditLog row per revoked family with the
    family_id + user_id in the payload.
    """
    from app.api.routers.auth.endpoints_sessions import logout_all
    from app.db.models import AuditLog as AuditLogModel

    user = await user_factory(telegram_user_id=987654328, username="audit-logout-all")

    await create_refresh_token(user.telegram_user_id, "mobile-app")
    await create_refresh_token(user.telegram_user_id, "desktop-app")

    response = MagicMock()
    current_user = {"user_id": user.telegram_user_id}
    auth_repo = get_auth_repository()

    await logout_all(response=response, current_user=current_user, auth_repo=auth_repo)

    async with db.session() as session:
        audit_rows = list(
            (
                await session.execute(
                    sa_select(AuditLogModel).where(AuditLogModel.event == "refresh_family_revoked")
                )
            )
            .scalars()
            .all()
        )

    # One audit row per family.
    assert len(audit_rows) == 2
    for row in audit_rows:
        assert row.details_json is not None
        assert row.details_json.get("user_id") == user.telegram_user_id
        assert row.details_json.get("reason") == "logout_all"
        assert row.details_json.get("family_id") is not None
