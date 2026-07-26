"""Comprehensive tests for forwarded message handling.

Covers ForwardContentProcessor (attribution, empty text guard, dedup),
ForwardProcessor (cached summary branches + exception handling +
custom-article edges), ForwardSummarizer (truncation, language, token
calc), MessageRouter forward routing edges, MessagePersistence forward
defaults, and TelegramMessage.is_forwarded detection.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.db.models import Request, TelegramMessage
from app.domain.models.request import RequestStatus
from app.infrastructure.persistence.message_persistence import MessagePersistence
from tests.conftest import make_test_app_config

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.session import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_forward_message(
    text: str | None = "Forward body",
    caption: str | None = None,
    fwd_chat_id: int | None = -100777,
    fwd_chat_title: str | None = "Test Channel",
    fwd_msg_id: int | None = 456,
    fwd_from_user: SimpleNamespace | None = None,
    fwd_sender_name: str | None = None,
    fwd_date: int | None = 1_700_000_100,
    user_id: int = 7,
    chat_id: int = 99,
) -> SimpleNamespace:
    fwd_chat = None
    if fwd_chat_id is not None:
        fwd_chat = SimpleNamespace(id=fwd_chat_id, type="channel", title=fwd_chat_title)

    return SimpleNamespace(
        id=321,
        message_id=321,
        text=text,
        caption=caption,
        entities=[],
        caption_entities=[],
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, username="tester"),
        forward_from_chat=fwd_chat,
        forward_from_message_id=fwd_msg_id,
        forward_from=fwd_from_user,
        forward_sender_name=fwd_sender_name,
        forward_date=fwd_date,
    )


def _forward_workflow_repo_kwargs() -> dict[str, MagicMock]:
    return {
        "summary_repo": MagicMock(),
        "request_repo": MagicMock(),
        "llm_repo": MagicMock(),
        "user_repo": MagicMock(),
    }


def _forward_processor_repo_kwargs() -> dict[str, MagicMock]:
    return {
        "summary_repo": MagicMock(),
        "request_repo": MagicMock(),
        "crawl_result_repo": MagicMock(),
        "llm_repo": MagicMock(),
        "user_repo": MagicMock(),
    }


def _make_processor(database: Database):
    from app.adapters.external.response_formatter import ResponseFormatter
    from app.adapters.telegram.forward_content_processor import ForwardContentProcessor

    cfg = make_test_app_config(db_path="/tmp/forward-test.db", allowed_user_ids=(1,))
    formatter = MagicMock(spec=ResponseFormatter)
    formatter.send_forward_accepted_notification = AsyncMock()
    formatter.send_forward_language_notification = AsyncMock()
    formatter.safe_reply = AsyncMock()

    processor = ForwardContentProcessor(
        cfg=cfg,
        db=database,
        response_formatter=formatter,
        audit_func=lambda *_a, **_kw: None,
    )
    return processor, formatter


def _make_workflow_cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.openrouter.temperature = 0.2
    cfg.openrouter.top_p = 1.0
    cfg.openrouter.model = "primary"
    cfg.openrouter.fallback_models = ()
    cfg.openrouter.structured_output_mode = "json_object"
    return cfg


# ===========================================================================
# ForwardContentProcessor: attribution
# ===========================================================================


async def test_channel_forward_uses_channel_label_and_title(database: Database) -> None:
    processor, _fmt = _make_processor(database)
    msg = _make_forward_message(text="Hello world", fwd_chat_title="My Channel")
    req_id, prompt, _lang, _sys = await processor.process_forward_content(msg, "cid")
    assert prompt.startswith("Channel: My Channel\n\n")
    assert "Hello world" in prompt
    assert req_id > 0


async def test_user_forward_uses_source_label_and_full_name(database: Database) -> None:
    processor, _fmt = _make_processor(database)
    fwd_user = SimpleNamespace(first_name="Jane", last_name="Doe")
    msg = _make_forward_message(
        text="User content",
        fwd_chat_id=None,
        fwd_msg_id=None,
        fwd_from_user=fwd_user,
    )
    _req_id, prompt, _lang, _sys = await processor.process_forward_content(msg, "cid")
    assert prompt.startswith("Source: Jane Doe\n\n")
    assert "User content" in prompt


async def test_user_forward_first_name_only(database: Database) -> None:
    processor, _fmt = _make_processor(database)
    fwd_user = SimpleNamespace(first_name="Alice", last_name=None)
    msg = _make_forward_message(
        text="First only",
        fwd_chat_id=None,
        fwd_msg_id=None,
        fwd_from_user=fwd_user,
    )
    _req_id, prompt, _lang, _sys = await processor.process_forward_content(msg, "cid")
    assert "Source: Alice\n\n" in prompt


async def test_privacy_protected_forward_uses_sender_name(database: Database) -> None:
    processor, _fmt = _make_processor(database)
    msg = _make_forward_message(
        text="Hidden content",
        fwd_chat_id=None,
        fwd_msg_id=None,
        fwd_from_user=None,
        fwd_sender_name="Anonymous Writer",
    )
    _req_id, prompt, _lang, _sys = await processor.process_forward_content(msg, "cid")
    assert prompt.startswith("Source: Anonymous Writer\n\n")


async def test_no_attribution_when_all_sources_missing(database: Database) -> None:
    processor, _fmt = _make_processor(database)
    msg = _make_forward_message(
        text="Orphan forward",
        fwd_chat_id=None,
        fwd_msg_id=None,
        fwd_from_user=None,
        fwd_sender_name=None,
    )
    _req_id, prompt, _lang, _sys = await processor.process_forward_content(msg, "cid")
    assert prompt == "Orphan forward"
    assert "Channel:" not in prompt
    assert "Source:" not in prompt


# ===========================================================================
# ForwardContentProcessor: empty text guard
# ===========================================================================


async def test_empty_text_raises_value_error(database: Database) -> None:
    processor, fmt = _make_processor(database)
    msg = _make_forward_message(text=None, caption=None)
    with pytest.raises(ValueError, match="no text content"):
        await processor.process_forward_content(msg, "cid")
    fmt.safe_reply.assert_awaited_once()
    reply_text = fmt.safe_reply.call_args[0][1]
    assert "no text content" in reply_text.lower()


async def test_whitespace_only_text_raises_value_error(database: Database) -> None:
    processor, _fmt = _make_processor(database)
    msg = _make_forward_message(text="   \n\t  ", caption=None)
    with pytest.raises(ValueError, match="no text content"):
        await processor.process_forward_content(msg, "cid")


async def test_caption_used_when_text_is_none(database: Database) -> None:
    processor, _fmt = _make_processor(database)
    msg = _make_forward_message(text=None, caption="Caption content here")
    _req_id, prompt, _lang, _sys = await processor.process_forward_content(msg, "cid")
    assert "Caption content here" in prompt


async def test_empty_string_text_uses_caption_fallback(database: Database) -> None:
    processor, _fmt = _make_processor(database)
    msg = _make_forward_message(text="", caption="Fallback caption")
    _req_id, prompt, _lang, _sys = await processor.process_forward_content(msg, "cid")
    assert "Fallback caption" in prompt


# ===========================================================================
# ForwardContentProcessor: embedded-link enrichment
# ===========================================================================


async def test_link_enricher_enriches_persisted_content_text(
    database: Database, session: AsyncSession
) -> None:
    from app.adapters.external.response_formatter import ResponseFormatter
    from app.adapters.telegram.forward_content_processor import ForwardContentProcessor

    cfg = make_test_app_config(db_path="/tmp/forward-test.db", allowed_user_ids=(1,))
    formatter = MagicMock(spec=ResponseFormatter)
    formatter.send_forward_accepted_notification = AsyncMock()
    formatter.send_forward_language_notification = AsyncMock()
    formatter.safe_reply = AsyncMock()

    enriched_text = (
        "Channel: Test Channel\n\nForward body\n\n"
        "## Referenced article: WSJ\nhttps://wsj.com/x\n\nfull article body"
    )
    enricher = AsyncMock()
    enricher.enrich = AsyncMock(return_value=enriched_text)

    processor = ForwardContentProcessor(
        cfg=cfg,
        db=database,
        response_formatter=formatter,
        audit_func=lambda *_a, **_kw: None,
        forward_link_enricher=enricher,
    )
    msg = _make_forward_message(text="Forward body", fwd_chat_title="Test Channel")

    req_id, prompt, _lang, _sys = await processor.process_forward_content(msg, "cid")

    # enricher invoked with the un-enriched prompt + post text
    enricher.enrich.assert_awaited_once()
    call = enricher.enrich.await_args
    assert call.kwargs["base_prompt"] == "Channel: Test Channel\n\nForward body"
    assert call.kwargs["post_text"] == "Forward body"
    assert call.kwargs["message"] is msg
    assert call.kwargs["correlation_id"] == "cid"

    # the enriched text is what flows downstream AND what gets persisted
    assert prompt == enriched_text
    row = await session.scalar(select(Request).where(Request.id == req_id))
    assert row is not None
    assert row.content_text == enriched_text


async def test_no_link_enricher_leaves_prompt_unenriched(database: Database) -> None:
    processor, _fmt = _make_processor(database)  # built without an enricher
    msg = _make_forward_message(text="Forward body", fwd_chat_title="Test Channel")
    _req_id, prompt, _lang, _sys = await processor.process_forward_content(msg, "cid")
    assert prompt == "Channel: Test Channel\n\nForward body"
    assert "## Referenced article" not in prompt


# ===========================================================================
# ForwardContentProcessor: dedup
# ===========================================================================


async def test_same_channel_forward_reuses_request(database: Database) -> None:
    processor, _fmt = _make_processor(database)
    msg = _make_forward_message(text="Channel post", fwd_chat_id=-100999, fwd_msg_id=42)

    req_id_1, _p1, _l1, _s1 = await processor.process_forward_content(msg, "cid-1")
    req_id_2, _p2, _l2, _s2 = await processor.process_forward_content(msg, "cid-2")

    assert req_id_1 == req_id_2


async def test_user_forward_no_dedup(database: Database) -> None:
    processor, _fmt = _make_processor(database)
    fwd_user = SimpleNamespace(first_name="Bob", last_name=None)
    msg = _make_forward_message(
        text="User message",
        fwd_chat_id=None,
        fwd_msg_id=None,
        fwd_from_user=fwd_user,
    )

    req_id_1, _p1, _l1, _s1 = await processor.process_forward_content(msg, "cid-a")
    req_id_2, _p2, _l2, _s2 = await processor.process_forward_content(msg, "cid-b")

    assert req_id_1 != req_id_2


async def test_forward_from_message_id_none_stored_as_null(
    database: Database, session: AsyncSession
) -> None:
    processor, _fmt = _make_processor(database)
    msg = _make_forward_message(
        text="No msg id",
        fwd_chat_id=None,
        fwd_msg_id=None,
        fwd_from_user=SimpleNamespace(first_name="X", last_name=None),
    )
    req_id, _p, _l, _s = await processor.process_forward_content(msg, "cid")

    row = await session.scalar(select(Request).where(Request.id == req_id))
    assert row is not None
    assert row.fwd_from_msg_id is None  # not 0


# ===========================================================================
# ForwardProcessor: cached summary branches
# ===========================================================================


def _make_forward_processor():
    from app.adapters.telegram.forward_processor import ForwardProcessor

    cfg = _make_workflow_cfg()
    db = MagicMock()
    openrouter = MagicMock()
    response_formatter = MagicMock()
    response_formatter.send_cached_summary_notification = AsyncMock()
    response_formatter.send_forward_summary_response = AsyncMock()

    audit_calls: list[tuple] = []

    processor = ForwardProcessor(
        cfg=cfg,
        db=db,
        openrouter=openrouter,
        response_formatter=response_formatter,
        audit_func=lambda *a, **_kw: audit_calls.append(a),
        sem=lambda: MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
        **_forward_processor_repo_kwargs(),
    )
    return processor, response_formatter, audit_calls


async def test_no_summary_row_returns_false() -> None:
    processor, fmt, _audit = _make_forward_processor()
    processor.summary_repo.async_get_summary_by_request = AsyncMock(return_value=None)
    result = await processor._maybe_reply_with_cached_summary(
        MagicMock(), 42, correlation_id="cid", interaction_id=None
    )
    assert result is False
    fmt.send_cached_summary_notification.assert_not_awaited()


async def test_empty_payload_returns_false() -> None:
    processor, _fmt, _audit = _make_forward_processor()
    processor.summary_repo.async_get_summary_by_request = AsyncMock(
        return_value={"json_payload": None}
    )
    result = await processor._maybe_reply_with_cached_summary(
        MagicMock(), 42, correlation_id="cid", interaction_id=None
    )
    assert result is False


async def test_corrupted_json_returns_false() -> None:
    processor, _fmt, _audit = _make_forward_processor()
    processor.summary_repo.async_get_summary_by_request = AsyncMock(
        return_value={"json_payload": "not{json"}
    )
    result = await processor._maybe_reply_with_cached_summary(
        MagicMock(), 42, correlation_id="cid", interaction_id=None
    )
    assert result is False


async def test_valid_cache_hit_sends_notifications() -> None:
    processor, fmt, audit_calls = _make_forward_processor()
    payload = json.dumps({"summary_250": "cached", "tldr": "ok"})
    processor.summary_repo.async_get_summary_by_request = AsyncMock(
        return_value={"json_payload": payload}
    )
    processor.request_repo.async_update_request_status = AsyncMock()

    msg = MagicMock()
    result = await processor._maybe_reply_with_cached_summary(
        msg, 42, correlation_id="cid", interaction_id=None
    )

    assert result is True
    fmt.send_cached_summary_notification.assert_awaited_once_with(msg)
    fmt.send_forward_summary_response.assert_awaited_once()
    processor.request_repo.async_update_request_status.assert_awaited_once_with(42, "ok")
    assert any("forward_summary_cache_hit" in str(c) for c in audit_calls)


async def test_valid_cache_hit_updates_interaction() -> None:
    processor, _fmt, _audit = _make_forward_processor()
    payload = json.dumps({"summary_250": "cached"})
    processor.summary_repo.async_get_summary_by_request = AsyncMock(
        return_value={"json_payload": payload}
    )
    processor.request_repo.async_update_request_status = AsyncMock()

    with patch(
        "app.adapters.telegram.forward_processor.async_safe_update_user_interaction"
    ) as mock_update:
        mock_update.return_value = None
        result = await processor._maybe_reply_with_cached_summary(
            MagicMock(), 42, correlation_id="cid", interaction_id=99
        )

    assert result is True
    mock_update.assert_awaited_once()
    call_kwargs = mock_update.call_args.kwargs
    assert call_kwargs["interaction_id"] == 99
    assert call_kwargs["response_sent"] is True


# ===========================================================================
# ForwardProcessor: exception handling
# ===========================================================================


async def test_content_processor_error_caught() -> None:
    from app.adapters.telegram.forward_processor import ForwardProcessor

    processor = ForwardProcessor(
        cfg=_make_workflow_cfg(),
        db=MagicMock(),
        openrouter=MagicMock(),
        response_formatter=MagicMock(),
        audit_func=lambda *_a, **_kw: None,
        sem=lambda: MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
        **_forward_processor_repo_kwargs(),
    )

    processor.content_processor.process_forward_content = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValueError("Forwarded message has no text content")
    )

    await processor.handle_forward_flow(MagicMock(), correlation_id="cid", interaction_id=None)


async def test_summarizer_error_caught() -> None:
    from app.adapters.telegram.forward_processor import ForwardProcessor

    processor = ForwardProcessor(
        cfg=_make_workflow_cfg(),
        db=MagicMock(),
        openrouter=MagicMock(),
        response_formatter=MagicMock(),
        audit_func=lambda *_a, **_kw: None,
        sem=lambda: MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
        **_forward_processor_repo_kwargs(),
    )

    processor.content_processor.process_forward_content = AsyncMock(  # type: ignore[method-assign]
        return_value=(1, "prompt", "en", "sys")
    )
    processor._maybe_reply_with_cached_summary = AsyncMock(return_value=False)  # type: ignore[method-assign]
    processor.summarizer.summarize_forward = AsyncMock(side_effect=RuntimeError("LLM timeout"))  # type: ignore[method-assign]

    await processor.handle_forward_flow(MagicMock(), correlation_id="cid", interaction_id=None)


# ===========================================================================
# ForwardProcessor: standalone-article follow-up suppressed for forwards
# ===========================================================================
# The forward summary card already carries TL;DR, tags, entities and
# categories, so generating an additional "standalone article from topics &
# tags" duplicates work the user just received. Worse, the background LLM
# call can stall (e.g. qwen-flash 422 on structured outputs) leaving the
# "Crafting a standalone article…" notice with no follow-up. Drop the
# scheduling entirely on the forward path -- the URL path keeps its own
# article flow.


async def test_handle_forward_flow_does_not_schedule_custom_article() -> None:
    """A successful forward summary must NOT trigger the standalone-article
    flow -- no 'Crafting…' notice, no extra LLM call."""
    from app.adapters.telegram.forward_processor import ForwardProcessor

    response_formatter = MagicMock()
    response_formatter.safe_reply = AsyncMock()
    response_formatter.send_forward_summary_response = AsyncMock()

    processor = ForwardProcessor(
        cfg=_make_workflow_cfg(),
        db=MagicMock(),
        openrouter=MagicMock(),
        response_formatter=response_formatter,
        audit_func=lambda *_a, **_kw: None,
        sem=lambda: MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
        **_forward_processor_repo_kwargs(),
    )

    processor.content_processor.process_forward_content = AsyncMock(  # type: ignore[method-assign]
        return_value=(1, "prompt", "en", "sys")
    )
    processor._maybe_reply_with_cached_summary = AsyncMock(return_value=False)  # type: ignore[method-assign]
    # A complete summary with topics + tags -- the exact case the old code
    # would have used to schedule a standalone-article generation.
    summary_payload: dict[str, Any] = {
        "key_ideas": ["topic one", "topic two"],
        "topic_tags": ["#tag1", "#tag2"],
    }
    processor.summarizer.summarize_forward = AsyncMock(return_value=summary_payload)  # type: ignore[method-assign]

    await processor.handle_forward_flow(MagicMock(), correlation_id="cid", interaction_id=None)

    # The "Crafting a standalone article…" notice is the only place
    # safe_reply is fired from the custom-article flow. If it never fires,
    # the flow was not scheduled.
    crafting_calls = [
        call
        for call in response_formatter.safe_reply.await_args_list
        if len(call.args) >= 2 and "Crafting a standalone article" in str(call.args[1])
    ]
    assert crafting_calls == [], (
        "Forward flow must not send the 'Crafting…' notice; "
        f"got {len(crafting_calls)} crafting reply(ies)"
    )

    # And the method itself must be gone from the public surface of the
    # processor -- not just unscheduled but removed, so future contributors
    # don't accidentally wire it back in.
    assert not hasattr(processor, "_maybe_generate_custom_article")


# ===========================================================================
# ForwardSummarizer: truncation, language, token calc
# ===========================================================================


def _make_summarizer():
    from app.adapters.telegram.forward_summarizer import ForwardSummarizer

    return ForwardSummarizer(
        cfg=_make_workflow_cfg(),
        db=MagicMock(),
        openrouter=MagicMock(),
        response_formatter=MagicMock(),
        audit_func=lambda *_a, **_kw: None,
        sem=lambda: MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
        **_forward_workflow_repo_kwargs(),
    )


async def test_long_prompt_truncated() -> None:
    summarizer = _make_summarizer()
    long_prompt = "A" * 50000
    mock_workflow = AsyncMock(return_value={"summary_250": "ok"})

    with patch.object(summarizer._workflow, "execute_summary_workflow", new=mock_workflow):
        await summarizer.summarize_forward(MagicMock(), long_prompt, "en", "sys", 1, "cid", None)

    user_content = mock_workflow.call_args.kwargs["requests"][0].messages[1]["content"]
    assert "[Content truncated due to length]" in user_content


async def test_short_prompt_not_truncated() -> None:
    summarizer = _make_summarizer()
    mock_workflow = AsyncMock(return_value={"summary_250": "ok"})

    with patch.object(summarizer._workflow, "execute_summary_workflow", new=mock_workflow):
        await summarizer.summarize_forward(
            MagicMock(), "Short message", "en", "sys", 1, "cid", None
        )

    user_content = mock_workflow.call_args.kwargs["requests"][0].messages[1]["content"]
    assert "[Content truncated" not in user_content
    assert "Short message" in user_content


async def test_russian_language_prompt() -> None:
    summarizer = _make_summarizer()
    mock_workflow = AsyncMock(return_value={"summary_250": "ok"})

    with patch.object(summarizer._workflow, "execute_summary_workflow", new=mock_workflow):
        await summarizer.summarize_forward(
            MagicMock(), "Русский текст", "ru", "sys", 1, "cid", None
        )

    user_content = mock_workflow.call_args.kwargs["requests"][0].messages[1]["content"]
    assert "Russian" in user_content
    assert "English" not in user_content


async def test_english_language_prompt() -> None:
    summarizer = _make_summarizer()
    mock_workflow = AsyncMock(return_value={"summary_250": "ok"})

    with patch.object(summarizer._workflow, "execute_summary_workflow", new=mock_workflow):
        await summarizer.summarize_forward(MagicMock(), "English text", "en", "sys", 1, "cid", None)

    user_content = mock_workflow.call_args.kwargs["requests"][0].messages[1]["content"]
    assert "English" in user_content
    assert "Russian" not in user_content


async def test_token_calculation() -> None:
    """``max_tokens`` is the OUTPUT budget for the structured summary JSON
    schema; it must NOT scale with prompt length. A flat 6144 ensures even a
    one-line forward gets the full schema-sized response budget (the prior
    prompt-length-derived formula gave short forwards ~2k tokens, causing
    truncation → ``truncation_recovery_skipped_budget_tight``).
    """
    summarizer = _make_summarizer()
    mock_workflow = AsyncMock(return_value=None)

    with patch.object(summarizer._workflow, "execute_summary_workflow", new=mock_workflow):
        await summarizer.summarize_forward(MagicMock(), "short", "en", "sys", 1, "cid", None)
    assert mock_workflow.call_args.kwargs["requests"][0].max_tokens == 6144

    long_text = "X" * 20000
    mock_workflow.reset_mock()
    with patch.object(summarizer._workflow, "execute_summary_workflow", new=mock_workflow):
        await summarizer.summarize_forward(MagicMock(), long_text, "en", "sys", 1, "cid", None)
    assert mock_workflow.call_args.kwargs["requests"][0].max_tokens == 6144


# ===========================================================================
# MessageRouter: forward routing edges
# ===========================================================================


async def test_forward_caption_only_routes_to_forward_flow(database: Database) -> None:
    from app.adapters.telegram.message_router import MessageRouter

    cfg = make_test_app_config(db_path="/tmp/forward-router.db")
    cfg = replace(
        cfg,
        runtime=cfg.runtime.model_copy(update={"aggregate_coalesce_enabled": False}),
    )

    url_handler: Any = SimpleNamespace(
        url_processor=MagicMock(),
        is_awaiting_url=MagicMock(return_value=False),
        handle_awaited_url=AsyncMock(),
        handle_direct_url=AsyncMock(),
        add_awaiting_user=MagicMock(),
        can_handle_document=MagicMock(return_value=False),
        handle_document_file=AsyncMock(),
    )
    forward_processor: Any = SimpleNamespace(handle_forward_flow=AsyncMock())
    response_formatter: Any = SimpleNamespace(
        safe_reply=AsyncMock(),
        send_error_notification=AsyncMock(),
    )

    router = MessageRouter(
        cfg=cfg,
        db=database,
        access_controller=SimpleNamespace(check_access=AsyncMock(return_value=True)),  # type: ignore[arg-type]
        command_processor=MagicMock(),
        url_handler=url_handler,
        forward_processor=forward_processor,
        response_formatter=response_formatter,
        audit_func=lambda *_a, **_kw: None,
    )

    message = SimpleNamespace(
        id=700,
        chat=SimpleNamespace(id=777),
        from_user=SimpleNamespace(id=1, is_bot=False),
        outgoing=False,
        text=None,
        caption="Photo caption with content",
        contact=None,
        web_app_data=None,
        photo=None,
        document=None,
        forward_from=SimpleNamespace(id=1111, first_name="Captioner", last_name=None),
        forward_from_chat=None,
        forward_from_message_id=None,
        forward_sender_name=None,
        forward_date=1700000000,
    )

    await router.route_message(message)

    forward_processor.handle_forward_flow.assert_awaited_once()
    url_handler.handle_direct_url.assert_not_awaited()


async def test_channel_forward_missing_msg_id_falls_to_user_path(database: Database) -> None:
    from app.adapters.telegram.message_router import MessageRouter

    cfg = make_test_app_config(db_path="/tmp/forward-router2.db")
    cfg = replace(
        cfg,
        runtime=cfg.runtime.model_copy(update={"aggregate_coalesce_enabled": False}),
    )

    url_handler: Any = SimpleNamespace(
        url_processor=MagicMock(),
        is_awaiting_url=AsyncMock(return_value=False),
        handle_awaited_url=AsyncMock(),
        handle_direct_url=AsyncMock(),
        add_awaiting_user=MagicMock(),
        can_handle_document=MagicMock(return_value=False),
        handle_document_file=AsyncMock(),
    )
    forward_processor: Any = SimpleNamespace(handle_forward_flow=AsyncMock())
    response_formatter: Any = SimpleNamespace(
        safe_reply=AsyncMock(),
        send_error_notification=AsyncMock(),
    )

    router = MessageRouter(
        cfg=cfg,
        db=database,
        access_controller=SimpleNamespace(check_access=AsyncMock(return_value=True)),  # type: ignore[arg-type]
        command_processor=MagicMock(),
        url_handler=url_handler,
        forward_processor=forward_processor,
        response_formatter=response_formatter,
        audit_func=lambda *_a, **_kw: None,
    )

    message = SimpleNamespace(
        id=701,
        chat=SimpleNamespace(id=778),
        from_user=SimpleNamespace(id=1, is_bot=False),
        outgoing=False,
        text="Channel text without msg id",
        caption=None,
        contact=None,
        web_app_data=None,
        photo=None,
        document=None,
        forward_from_chat=SimpleNamespace(id=-100555, title="Restricted"),
        forward_from_message_id=None,
        forward_from=None,
        forward_sender_name=None,
        forward_date=1700000000,
    )

    await router.route_message(message)
    assert forward_processor.handle_forward_flow.await_count == 1


# ===========================================================================
# MessagePersistence: forward_from_message_id default fix
# ===========================================================================


async def test_persist_forward_from_message_id_none_stored_as_null(
    database: Database, session: AsyncSession
) -> None:
    persistence = MessagePersistence(database)

    msg = _make_forward_message(
        text="Content",
        fwd_chat_id=None,
        fwd_msg_id=None,
        fwd_from_user=SimpleNamespace(first_name="U", last_name=None),
    )

    req_id = await persistence.request_repo.async_create_request(
        type_="forward",
        status=RequestStatus.PENDING,
        correlation_id="cid",
        chat_id=99,
        user_id=7,
    )

    await persistence.persist_message_snapshot(req_id, msg)

    row = await session.scalar(select(TelegramMessage).where(TelegramMessage.request_id == req_id))
    assert row is not None
    assert row.forward_from_message_id is None


async def test_persist_forward_from_message_id_present_stored_correctly(
    database: Database, session: AsyncSession
) -> None:
    persistence = MessagePersistence(database)

    msg = _make_forward_message(text="Content", fwd_chat_id=-100, fwd_msg_id=456)

    req_id = await persistence.request_repo.async_create_request(
        type_="forward",
        status=RequestStatus.PENDING,
        correlation_id="cid",
        chat_id=99,
        user_id=7,
        fwd_from_chat_id=-100,
        fwd_from_msg_id=456,
    )

    await persistence.persist_message_snapshot(req_id, msg)

    row = await session.scalar(select(TelegramMessage).where(TelegramMessage.request_id == req_id))
    assert row is not None
    assert row.forward_from_message_id == 456


# ===========================================================================
# TelegramMessage.is_forwarded detection
# ===========================================================================


def _make_mock_message(**overrides: Any) -> SimpleNamespace:
    from datetime import datetime

    defaults: dict[str, Any] = {
        "id": 1,
        "date": datetime.now(),
        "text": "test",
        "caption": None,
        "entities": [],
        "caption_entities": [],
        "photo": None,
        "video": None,
        "audio": None,
        "document": None,
        "sticker": None,
        "voice": None,
        "video_note": None,
        "animation": None,
        "contact": None,
        "location": None,
        "venue": None,
        "poll": None,
        "dice": None,
        "game": None,
        "invoice": None,
        "successful_payment": None,
        "story": None,
        "forward_from": None,
        "forward_from_chat": None,
        "forward_from_message_id": None,
        "forward_signature": None,
        "forward_sender_name": None,
        "forward_date": None,
        "reply_to_message": None,
        "edit_date": None,
        "media_group_id": None,
        "author_signature": None,
        "via_bot": None,
        "has_protected_content": None,
        "connected_website": None,
        "reply_markup": None,
        "views": None,
        "via_bot_user_id": None,
        "effect_id": None,
        "link_preview_options": None,
        "show_caption_above_media": None,
        "from_user": None,
        "chat": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_regular_message_not_forwarded() -> None:
    from app.adapter_models.telegram.telegram_message import TelegramMessage

    tm = TelegramMessage.from_telegram_message(_make_mock_message())
    assert not tm.is_forwarded


def test_forward_from_user_detected() -> None:
    from app.adapter_models.telegram.telegram_message import TelegramMessage

    msg = _make_mock_message(
        forward_from=SimpleNamespace(
            id=1,
            is_bot=False,
            first_name="A",
            last_name=None,
            username=None,
            language_code=None,
        )
    )
    assert TelegramMessage.from_telegram_message(msg).is_forwarded


def test_forward_from_chat_detected() -> None:
    from app.adapter_models.telegram.telegram_message import TelegramMessage

    msg = _make_mock_message(
        forward_from_chat=SimpleNamespace(id=-100, type="channel", title="Ch"),
        forward_from_message_id=42,
    )
    assert TelegramMessage.from_telegram_message(msg).is_forwarded


def test_forward_sender_name_only_detected() -> None:
    from app.adapter_models.telegram.telegram_message import TelegramMessage

    msg = _make_mock_message(forward_sender_name="Hidden")
    assert TelegramMessage.from_telegram_message(msg).is_forwarded


def test_forward_date_only_detected() -> None:
    from datetime import datetime

    from app.adapter_models.telegram.telegram_message import TelegramMessage

    msg = _make_mock_message(forward_date=datetime(2024, 1, 1))
    assert TelegramMessage.from_telegram_message(msg).is_forwarded
