from __future__ import annotations

import datetime as dt
import os
from typing import cast

import pytest
from sqlalchemy import Table, select, text

from app.config.database import DatabaseConfig
from app.db.base import Base
from app.db.models import (
    ALL_MODELS,
    CORE_MODELS,
    AttachmentProcessing,
    AudioGeneration,
    AuditLog,
    Chat,
    ClientSecret,
    CrawlResult,
    LLMCall,
    RefreshToken,
    Request,
    Summary,
    SummaryEmbedding,
    TelegramMessage,
    User,
    UserDevice,
    UserInteraction,
    VideoDownload,
)
from app.db.session import Database
from app.db.types import _utcnow, model_to_dict

pytestmark = pytest.mark.postgres


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


def _core_tables() -> list[Table]:
    return [cast("Table", model.__table__) for model in reversed(ALL_MODELS)]


@pytest.mark.asyncio
async def test_core_models_round_trip_against_postgres() -> None:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres model smoke test")

    database = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    now = _utcnow()
    expires_at = now + dt.timedelta(days=7)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all, tables=_core_tables())
            await connection.run_sync(
                Base.metadata.create_all, tables=list(reversed(_core_tables()))
            )

        async with database.transaction() as session:
            user = User(
                telegram_user_id=101,
                username="alice",
                is_owner=True,
                preferences_json={"theme": "frost"},
            )
            chat = Chat(chat_id=202, type="private", title="Alice", username="alice_chat")
            request = Request(
                type="url",
                status="done",
                correlation_id="corr-core",
                chat_id=chat.chat_id,
                user_id=user.telegram_user_id,
                input_url="https://example.com",
                normalized_url="https://example.com/",
                dedupe_hash="core-dedupe",
                error_context_json={"source": "test"},
            )
            session.add_all([user, chat, request])
            await session.flush()

            summary = Summary(
                request_id=request.id,
                lang="en",
                json_payload={"summary_250": "short"},
                insights_json=["insight"],
                is_read=True,
            )
            session.add(summary)
            await session.flush()

            session.add_all(
                [
                    ClientSecret(
                        user_id=user.telegram_user_id,
                        client_id="web-v1",
                        secret_hash="hash",
                        secret_salt="salt",
                    ),
                    TelegramMessage(
                        request_id=request.id,
                        message_id=1,
                        chat_id=chat.chat_id,
                        text_full="hello",
                        entities_json=[{"type": "url"}],
                    ),
                    CrawlResult(
                        request_id=request.id,
                        source_url="https://example.com",
                        status="ok",
                        options_json={"mode": "fast"},
                    ),
                    LLMCall(
                        request_id=request.id,
                        provider="openrouter",
                        model="test-model",
                        response_json={"ok": True},
                    ),
                    UserInteraction(
                        user_id=user.telegram_user_id,
                        interaction_type="message",
                        request_id=request.id,
                    ),
                    AuditLog(level="INFO", event="core_model_test", details_json={"ok": True}),
                    SummaryEmbedding(
                        summary_id=summary.id,
                        model_name="embedding",
                        model_version="1",
                        embedding_blob=b"1234",
                        dimensions=2,
                        language="en",
                    ),
                    VideoDownload(request_id=request.id, video_id="abc123", status="done"),
                    AudioGeneration(
                        summary_id=summary.id,
                        voice_id="voice",
                        model="tts",
                        status="done",
                    ),
                    AttachmentProcessing(
                        request_id=request.id,
                        file_type="pdf",
                        status="done",
                    ),
                    UserDevice(
                        user_id=user.telegram_user_id,
                        token="push-token",
                        platform="ios",
                    ),
                    RefreshToken(
                        user_id=user.telegram_user_id,
                        token_hash="refresh-hash",
                        family_id="refresh-family",
                        expires_at=expires_at,
                    ),
                ]
            )

        async with database.session() as session:
            stored_user = await session.scalar(
                select(User).where(User.telegram_user_id == user.telegram_user_id)
            )
            stored_request = await session.scalar(select(Request).where(Request.id == request.id))
            stored_summary = await session.scalar(select(Summary).where(Summary.id == summary.id))

        assert stored_user is not None
        assert stored_request is not None
        assert stored_summary is not None
        assert stored_user.preferences_json == {"theme": "frost"}
        assert stored_request.error_context_json == {"source": "test"}
        assert stored_summary.json_payload == {"summary_250": "short"}
        assert model_to_dict(stored_user)["telegram_user_id"] == 101
        assert {model.__name__ for model in CORE_MODELS} == {
            "AttachmentProcessing",
            "AudioGeneration",
            "AuditLog",
            "Chat",
            "ClientSecret",
            "CrawlResult",
            "LLMCall",
            "MagicLinkToken",
            "ProgressEvent",
            "RefreshToken",
            "Request",
            "RequestProcessingJob",
            "Summary",
            "SummaryEmbedding",
            "TelegramMessage",
            "User",
            "UserCredential",
            "UserDevice",
            "UserIdentity",
            "UserInteraction",
            "VideoDownload",
            "XBookmarkMetadata",
        }
    finally:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all, tables=_core_tables())
            await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await database.dispose()


@pytest.mark.asyncio
async def test_server_version_before_update_is_monotonic_against_postgres() -> None:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres model smoke test")

    database = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all, tables=_core_tables())
            await connection.run_sync(
                Base.metadata.create_all, tables=list(reversed(_core_tables()))
            )

        async with database.transaction() as session:
            request = Request(type="url", status="pending")
            session.add(request)
            await session.flush()
            original_version = request.server_version
            request.status = "done"

        async with database.session() as session:
            stored_request = await session.scalar(select(Request).where(Request.id == request.id))

        assert stored_request is not None
        assert stored_request.server_version > original_version
    finally:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all, tables=_core_tables())
            await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await database.dispose()


@pytest.mark.asyncio
async def test_server_version_before_update_outpaces_stale_memory_and_clock() -> None:
    """The before_update guard must never regress a concurrently-advanced row.

    Simulates a session that loaded a row (capturing a now-stale in-memory
    ``server_version``) while a second writer commits a far-future
    ``server_version`` on the same row in between. The guard must read the
    row's *current* committed value -- not the stale in-memory one -- so its
    own update lands strictly past what the concurrent writer committed, even
    though the wall-clock-derived candidate is far behind that value.
    """
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres model smoke test")

    database = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all, tables=_core_tables())
            await connection.run_sync(
                Base.metadata.create_all, tables=list(reversed(_core_tables()))
            )

        async with database.transaction() as setup_session:
            request = Request(type="url", status="pending")
            setup_session.add(request)
            await setup_session.flush()
            request_id = request.id

        async with database.session() as session_a:
            loaded = await session_a.scalar(select(Request).where(Request.id == request_id))
            assert loaded is not None
            stale_version = loaded.server_version

            # Far ahead of any wall-clock-derived candidate the guard could
            # compute for "now", so the fix must take the current+1 branch.
            far_future_version = stale_version + 10_000_000
            async with database.transaction() as writer_session:
                await writer_session.execute(
                    text("UPDATE requests SET server_version = :v WHERE id = :id"),
                    {"v": far_future_version, "id": request_id},
                )

            # session_a's in-memory copy is still the pre-race value.
            assert loaded.server_version == stale_version

            loaded.status = "done"
            await session_a.commit()

        async with database.session() as verify_session:
            stored_request = await verify_session.scalar(
                select(Request).where(Request.id == request_id)
            )

        assert stored_request is not None
        assert stored_request.server_version > far_future_version
    finally:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all, tables=_core_tables())
            await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await database.dispose()
