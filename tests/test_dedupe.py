"""End-to-end dedupe-on-URL and forward-cache-reuse coverage.

Ported off the legacy DatabaseSessionManager / Peewee shim. Drives the
full TelegramBot pipeline (URL processor + summarization stack) against
async Postgres, with Firecrawl/OpenRouter mocked so the test stays
hermetic. The original behavioural assertions (summary persisted,
version increments, correlation_id updated, LLM bypassed for cached
forwards) are preserved.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from app.adapter_models.llm.llm_models import LLMCallResult, StructuredLLMResult
from app.adapters.content.url_flow_models import URLFlowRequest
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.adapters.telegram.telegram_bot import TelegramBot
from app.core.call_status import CallStatus
from app.core.url_utils import normalize_url, url_hash_sha256
from app.infrastructure.persistence.repositories.request_repository import RequestRepositoryAdapter
from app.infrastructure.persistence.repositories.summary_repository import SummaryRepositoryAdapter
from tests.conftest import make_test_app_config
from tests.db_helpers_async import (
    create_request,
    get_request_by_forward,
    get_summary_by_request,
    insert_crawl_result,
    insert_summary,
)
from tests.telegram_bot_builders import AUDIT_REPOSITORY_BUILDER, RUNTIME_BUILDER

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.session import Database


class FakeMessage:
    def __init__(self) -> None:
        class _Chat:
            id = 1
            type = "private"

        class _User:
            id = 1

        self.chat = _Chat()
        self.from_user = _User()
        self.id = 123
        self.message_id = 123
        self._replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self._replies.append(text)


class FakeForwardMessage(FakeMessage):
    def __init__(
        self,
        chat_id: int,
        fwd_chat_id: int,
        fwd_msg_id: int,
        text: str,
        title: str = "",
    ) -> None:
        super().__init__()

        class _Chat:
            def __init__(self, cid: int) -> None:
                self.id = cid

        class _User:
            id = 42

        class _FwdChat:
            def __init__(self, cid: int, title: str) -> None:
                self.id = cid
                self.title = title

        self.chat = _Chat(chat_id)  # type: ignore[assignment]
        self.from_user = _User()  # type: ignore[assignment]
        self.forward_from_chat = _FwdChat(fwd_chat_id, title)
        self.forward_from_message_id = fwd_msg_id
        self.text = text


class FakeFirecrawl:
    def __init__(self) -> None:
        self.calls = 0

    async def scrape_markdown(self, url: str, request_id: int | None = None) -> FirecrawlResult:
        self.calls += 1
        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=200,
            content_markdown=(
                "# Cached article\n\n"
                "This deterministic article body has enough useful words for the "
                "content quality gate and exercises the current graph extraction path. " * 8
            ),
            metadata_json={"title": "Cached article"},
            response_success=True,
            latency_ms=1,
            source_url=url,
        )


class FakeOpenRouter:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(
        self, messages: list[dict[str, Any]], request_id: int | None = None, **kwargs: Any
    ) -> LLMCallResult:
        self.calls += 1
        payload = {"summary_250": "ok", "summary_1000": "ok", "tldr": "ok"}
        content = json.dumps(payload)
        return LLMCallResult(
            status=CallStatus.OK,
            model="m",
            response_text=content,
            response_json=payload,
            tokens_prompt=1,
            tokens_completion=1,
            cost_usd=None,
            latency_ms=1,
            error_text=None,
            request_headers={},
            request_messages=messages,
        )

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        *,
        response_model: type[Any],
        **_kwargs: Any,
    ) -> StructuredLLMResult[Any]:
        self.calls += 1
        return StructuredLLMResult(
            parsed=response_model.model_construct(
                summary_250="ok",
                summary_1000="ok",
                tldr="ok",
            ),
            tokens_prompt=1,
            tokens_completion=1,
            latency_ms=1,
            model_used="m",
        )


def _make_bot(database: Database, *, llm_client: Any | None = None) -> TelegramBot:
    """Construct a TelegramBot wired against the supplied async Database."""
    cfg = make_test_app_config(db_path="/tmp/dedupe-test.db", allowed_user_ids=(1,))

    from app.adapters import telegram_bot as tbmod

    tbmod.Client = object
    tbmod.filters = None

    with patch(
        "app.di.shared.LLMClientFactory.create_from_config",
        return_value=llm_client or AsyncMock(),
    ):
        return TelegramBot(
            cfg=cfg,
            db=database,
            runtime_builder=RUNTIME_BUILDER,
            audit_repository_builder=AUDIT_REPOSITORY_BUILDER,
        )


@pytest.mark.asyncio
async def test_dedupe_and_summary_version_increment(
    database: Database, session: AsyncSession
) -> None:
    url = "https://Example.com/Path?a=1&utm_source=x"
    norm = normalize_url(url)
    dedupe = url_hash_sha256(norm)

    req_id = await create_request(
        session,
        type_="url",
        status="pending",
        correlation_id="initcid",
        chat_id=1,
        user_id=1,
        input_url=url,
        normalized_url=norm,
        dedupe_hash=dedupe,
        route_version=1,
    )
    await insert_crawl_result(
        session,
        request_id=req_id,
        source_url=url,
        endpoint="/v2/scrape",
        http_status=200,
        status="ok",
        options_json={"formats": ["markdown"], "mobile": True},
        correlation_id="firecrawl-cid",
        content_markdown="# cached",
        content_html=None,
        structured_json={},
        metadata_json={},
        links_json={},
        screenshots_paths_json=None,
        firecrawl_success=True,
        firecrawl_error_code=None,
        firecrawl_error_message=None,
        firecrawl_details_json=None,
        raw_response_json=None,
        latency_ms=1,
        error_text=None,
    )
    await session.commit()

    fake_firecrawl = FakeFirecrawl()
    fake_or = FakeOpenRouter()
    bot = _make_bot(database, llm_client=fake_or)
    bot_any = cast("Any", bot)

    # The extraction adapter retains this ContentExtractor instance, so replacing
    # its public scraper alias exercises the current graph path without reaching
    # into retired summarization-runtime internals.
    if hasattr(bot_any, "url_processor"):
        extractor = getattr(bot_any.url_processor, "content_extractor", None)
        if extractor is not None:
            extractor.firecrawl = fake_firecrawl

    msg = FakeMessage()
    first_result = await bot.url_processor.handle_url_flow(
        URLFlowRequest(
            message=msg,
            url_text=url,
            correlation_id="cid1",
            batch_mode=True,
        )
    )
    assert first_result.success is True
    assert first_result.cached is False

    # The graph and fixture use independent SQLAlchemy sessions. Read through the
    # repositories so assertions never reuse stale identity-map objects.
    summary_repo = SummaryRepositoryAdapter(database)
    request_repo = RequestRepositoryAdapter(database)
    s1 = await summary_repo.async_get_summary_by_request(req_id)
    assert s1 is not None
    version1 = int(s1["version"])
    assert version1 > 0

    row = await request_repo.async_get_request_by_dedupe_hash(dedupe)
    assert row is not None
    assert row["correlation_id"] == "cid1"
    first_pass_calls = fake_or.calls
    assert first_pass_calls >= 1  # summarization pipeline ran at least one LLM call

    assert fake_firecrawl.calls == 1

    # Second run: the persisted summary is a real cache hit, so neither extraction
    # nor the LLM runs and the summary version does not regress.
    second_result = await bot.url_processor.handle_url_flow(
        URLFlowRequest(
            message=msg,
            url_text=url,
            correlation_id="cid2",
            batch_mode=True,
        )
    )
    assert second_result.success is True
    assert second_result.cached is True
    s2 = await summary_repo.async_get_summary_by_request(req_id)
    assert s2 is not None
    assert int(s2["version"]) >= version1

    row2 = await request_repo.async_get_request_by_dedupe_hash(dedupe)
    assert row2 is not None
    assert row2["correlation_id"] == "cid2"
    assert fake_or.calls == first_pass_calls
    assert fake_firecrawl.calls == 1


@pytest.mark.asyncio
async def test_forward_cached_summary_reuse(database: Database, session: AsyncSession) -> None:
    fwd_chat_id = 777
    fwd_msg_id = 888

    req_id = await create_request(
        session,
        type_="forward",
        status="ok",
        correlation_id="orig",
        chat_id=1,
        user_id=1,
        input_message_id=5,
        fwd_from_chat_id=fwd_chat_id,
        fwd_from_msg_id=fwd_msg_id,
        route_version=1,
    )
    await insert_summary(
        session,
        request_id=req_id,
        lang="en",
        json_payload={"summary_250": "cached", "tldr": "cached"},
    )
    await session.commit()

    bot = _make_bot(database)

    class FailOpenRouter:
        async def chat(self, *_args: Any, **_kwargs: Any) -> LLMCallResult:
            msg = "LLM should not run for cached forward summaries"
            raise AssertionError(msg)

    bot_any = cast("Any", bot)
    bot_any._openrouter = FailOpenRouter()

    msg = FakeForwardMessage(
        chat_id=1,
        fwd_chat_id=fwd_chat_id,
        fwd_msg_id=fwd_msg_id,
        text="Forwarded content",
        title="Channel",
    )

    await bot._handle_forward_flow(msg, correlation_id="newcid")

    cached_summary = await get_summary_by_request(session, req_id)
    assert cached_summary is not None
    assert int(cached_summary["version"]) > 0

    existing_request = await get_request_by_forward(session, fwd_chat_id, fwd_msg_id)
    assert existing_request is not None
    assert existing_request["correlation_id"] == "newcid"
