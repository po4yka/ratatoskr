"""Async test helpers backed by SQLAlchemy 2.0.

These mirror the public API of `tests/db_helpers.py` but run against the
new SQLAlchemy `AsyncSession` instead of the legacy Peewee snapshot.
Every helper takes a `session: AsyncSession` as its first positional
argument; the rest of each signature is keyword-only and matches the
sync helper of the same name.

Use these from any test that has been migrated to the new async fixtures
(`session` / `database` / `db_helpers` in `tests/conftest.py`). Until a
caller test is migrated, it should keep using `tests/db_helpers.py` (the
shim that goes through `app.cli._legacy_peewee_models`).

When all callers have been migrated, this file replaces the old
`db_helpers.py` and the legacy snapshot consumption from `tests/` ends.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.time_utils import UTC
from app.db.json_utils import prepare_json_payload
from app.db.models import (
    AuditLog,
    Chat,
    CrawlResult,
    LLMCall,
    Request,
    Summary,
    TelegramMessage,
    User,
    UserInteraction,
    model_to_dict,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

JSONValue = Mapping[str, Any] | list[Any] | tuple[Any, ...] | str | None


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------


def _convert_bool_fields(data: dict[str, Any], fields: list[str]) -> None:
    for field_name in fields:
        if field_name in data and data[field_name] is not None:
            data[field_name] = int(bool(data[field_name]))


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


async def create_request(
    session: AsyncSession,
    *,
    type_: str,
    status: str,
    correlation_id: str | None = None,
    chat_id: int | None = None,
    user_id: int | None = None,
    input_url: str | None = None,
    normalized_url: str | None = None,
    dedupe_hash: str | None = None,
    input_message_id: int | None = None,
    fwd_from_chat_id: int | None = None,
    fwd_from_msg_id: int | None = None,
    lang_detected: str | None = None,
    content_text: str | None = None,
    route_version: int = 1,
) -> int:
    values = {
        "type": type_,
        "status": status,
        "correlation_id": correlation_id,
        "chat_id": chat_id,
        "user_id": user_id,
        "input_url": input_url,
        "normalized_url": normalized_url,
        "dedupe_hash": dedupe_hash,
        "input_message_id": input_message_id,
        "fwd_from_chat_id": fwd_from_chat_id,
        "fwd_from_msg_id": fwd_from_msg_id,
        "lang_detected": lang_detected,
        "content_text": content_text,
        "route_version": route_version,
    }
    if dedupe_hash is None:
        request = Request(**values)
        session.add(request)
        await session.flush()
        return int(request.id)

    update_values = {k: v for k, v in values.items() if k != "dedupe_hash"}
    if user_id is None:
        # PostgreSQL treats NULL values as distinct in the scoped
        # (user_id, dedupe_hash) unique index, so ON CONFLICT cannot match an
        # anonymous request. Preserve the legacy test-helper contract with an
        # explicit lookup/update; production requests normally carry user_id.
        existing_id = await session.scalar(
            select(Request.id).where(
                Request.user_id.is_(None),
                Request.dedupe_hash == dedupe_hash,
            )
        )
        if existing_id is not None:
            await session.execute(
                update(Request).where(Request.id == existing_id).values(**update_values)
            )
            return int(existing_id)

    stmt = (
        pg_insert(Request)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[Request.user_id, Request.dedupe_hash],
            index_where=Request.dedupe_hash.is_not(None),
            set_=update_values,
        )
        .returning(Request.id)
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def get_request_by_dedupe_hash(
    session: AsyncSession, dedupe_hash: str
) -> dict[str, Any] | None:
    request = await session.scalar(select(Request).where(Request.dedupe_hash == dedupe_hash))
    return model_to_dict(request)


async def get_request_by_forward(
    session: AsyncSession, fwd_chat_id: int, fwd_msg_id: int
) -> dict[str, Any] | None:
    request = await session.scalar(
        select(Request).where(
            Request.fwd_from_chat_id == fwd_chat_id,
            Request.fwd_from_msg_id == fwd_msg_id,
        )
    )
    return model_to_dict(request)


async def update_request_status(session: AsyncSession, request_id: int, status: str) -> None:
    await session.execute(update(Request).where(Request.id == request_id).values(status=status))


async def get_crawl_result_by_request(
    session: AsyncSession, request_id: int
) -> dict[str, Any] | None:
    result = await session.scalar(select(CrawlResult).where(CrawlResult.request_id == request_id))
    data = model_to_dict(result)
    if data:
        _convert_bool_fields(data, ["firecrawl_success"])
    return data


# ---------------------------------------------------------------------------
# Crawl / Telegram / LLM helpers
# ---------------------------------------------------------------------------


async def insert_crawl_result(
    session: AsyncSession,
    *,
    request_id: int,
    source_url: str | None = None,
    endpoint: str | None = None,
    http_status: int | None = None,
    status: str | None = None,
    options_json: JSONValue = None,
    correlation_id: str | None = None,
    content_markdown: str | None = None,
    content_html: str | None = None,
    structured_json: JSONValue = None,
    metadata_json: JSONValue = None,
    links_json: JSONValue = None,
    screenshots_paths_json: JSONValue = None,
    firecrawl_success: bool | None = None,
    firecrawl_error_code: str | None = None,
    firecrawl_error_message: str | None = None,
    firecrawl_details_json: JSONValue = None,
    raw_response_json: JSONValue = None,
    latency_ms: int | None = None,
    error_text: str | None = None,
) -> int:
    values = {
        "request_id": request_id,
        "source_url": source_url,
        "endpoint": endpoint,
        "http_status": http_status,
        "status": status,
        "options_json": prepare_json_payload(options_json, default={}),
        "correlation_id": correlation_id,
        "content_markdown": content_markdown,
        "content_html": content_html,
        "structured_json": prepare_json_payload(structured_json, default={}),
        "metadata_json": prepare_json_payload(metadata_json, default={}),
        "links_json": prepare_json_payload(links_json, default={}),
        "screenshots_paths_json": prepare_json_payload(screenshots_paths_json),
        "firecrawl_success": firecrawl_success,
        "firecrawl_error_code": firecrawl_error_code,
        "firecrawl_error_message": firecrawl_error_message,
        "firecrawl_details_json": prepare_json_payload(firecrawl_details_json),
        "raw_response_json": prepare_json_payload(raw_response_json),
        "latency_ms": latency_ms,
        "error_text": error_text,
    }
    stmt = (
        pg_insert(CrawlResult)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[CrawlResult.request_id])
        .returning(CrawlResult.id)
    )
    result = await session.execute(stmt)
    inserted_id = result.scalar()
    if inserted_id is not None:
        return int(inserted_id)
    existing = await session.scalar(
        select(CrawlResult.id).where(CrawlResult.request_id == request_id)
    )
    return int(existing) if existing is not None else 0


async def insert_telegram_message(
    session: AsyncSession,
    *,
    request_id: int,
    message_id: int | None = None,
    chat_id: int | None = None,
    date_ts: int | None = None,
    text_full: str | None = None,
    entities_json: JSONValue = None,
    media_type: str | None = None,
    media_file_ids_json: JSONValue = None,
    forward_from_chat_id: int | None = None,
    forward_from_chat_type: str | None = None,
    forward_from_chat_title: str | None = None,
    forward_from_message_id: int | None = None,
    forward_date_ts: int | None = None,
    telegram_raw_json: JSONValue = None,
) -> int:
    values = {
        "request_id": request_id,
        "message_id": message_id,
        "chat_id": chat_id,
        "date_ts": date_ts,
        "text_full": text_full,
        "entities_json": prepare_json_payload(entities_json),
        "media_type": media_type,
        "media_file_ids_json": prepare_json_payload(media_file_ids_json),
        "forward_from_chat_id": forward_from_chat_id,
        "forward_from_chat_type": forward_from_chat_type,
        "forward_from_chat_title": forward_from_chat_title,
        "forward_from_message_id": forward_from_message_id,
        "forward_date_ts": forward_date_ts,
        "telegram_raw_json": prepare_json_payload(telegram_raw_json),
    }
    stmt = (
        pg_insert(TelegramMessage)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[TelegramMessage.request_id])
        .returning(TelegramMessage.id)
    )
    result = await session.execute(stmt)
    inserted_id = result.scalar()
    if inserted_id is not None:
        return int(inserted_id)
    existing = await session.scalar(
        select(TelegramMessage.id).where(TelegramMessage.request_id == request_id)
    )
    return int(existing) if existing is not None else 0


async def insert_llm_call(
    session: AsyncSession,
    *,
    request_id: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    request_headers_json: JSONValue = None,
    request_messages_json: JSONValue = None,
    response_text: str | None = None,
    response_json: JSONValue = None,
    tokens_prompt: int | None = None,
    tokens_completion: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
    status: str | None = None,
    error_text: str | None = None,
    structured_output_used: bool | None = None,
    structured_output_mode: str | None = None,
    error_context_json: JSONValue = None,
) -> int:
    values: dict[str, Any] = {
        "request_id": request_id,
        "provider": provider,
        "model": model,
        "endpoint": endpoint,
        "request_headers_json": prepare_json_payload(request_headers_json, default={}),
        "request_messages_json": prepare_json_payload(request_messages_json, default=[]),
        "tokens_prompt": tokens_prompt,
        "tokens_completion": tokens_completion,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "status": status,
        "error_text": error_text,
        "structured_output_used": structured_output_used,
        "structured_output_mode": structured_output_mode,
        "error_context_json": prepare_json_payload(error_context_json),
    }
    response_payload = prepare_json_payload(response_json, default={})
    if provider == "openrouter":
        values["openrouter_response_text"] = response_text
        values["openrouter_response_json"] = response_payload
        values["response_text"] = None
        values["response_json"] = None
    else:
        values["response_text"] = response_text
        values["response_json"] = response_payload

    call = LLMCall(**values)
    session.add(call)
    await session.flush()
    return int(call.id)


# ---------------------------------------------------------------------------
# User / Chat helpers
# ---------------------------------------------------------------------------


async def upsert_user(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    username: str | None = None,
    is_owner: bool = False,
) -> None:
    stmt = pg_insert(User).values(
        telegram_user_id=telegram_user_id,
        username=username,
        is_owner=is_owner,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.telegram_user_id],
        set_={"username": username, "is_owner": is_owner},
    )
    await session.execute(stmt)


async def upsert_chat(
    session: AsyncSession,
    *,
    chat_id: int,
    type_: str,
    title: str | None = None,
    username: str | None = None,
) -> None:
    stmt = pg_insert(Chat).values(
        chat_id=chat_id,
        type=type_,
        title=title,
        username=username,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Chat.chat_id],
        set_={"type": type_, "title": title, "username": username},
    )
    await session.execute(stmt)


async def update_user_interaction(
    session: AsyncSession,
    interaction_id: int,
    *,
    updates: Mapping[str, Any] | None = None,
    response_sent: bool | None = None,
    response_type: str | None = None,
    error_occurred: bool | None = None,
    error_message: str | None = None,
    processing_time_ms: int | None = None,
    request_id: int | None = None,
) -> None:
    legacy_fields = (
        response_sent,
        response_type,
        error_occurred,
        error_message,
        processing_time_ms,
        request_id,
    )
    if updates and any(f is not None for f in legacy_fields):
        msg = "Cannot mix explicit field arguments with the updates mapping"
        raise ValueError(msg)

    update_values: dict[str, Any] = {}
    if updates:
        valid_columns = {col.key for col in UserInteraction.__table__.columns}
        invalid = [k for k in updates if k not in valid_columns]
        if invalid:
            msg = f"Unknown user interaction fields: {', '.join(invalid)}"
            raise ValueError(msg)
        update_values.update(updates)

    if response_sent is not None:
        update_values["response_sent"] = response_sent
    if response_type is not None:
        update_values["response_type"] = response_type
    if error_occurred is not None:
        update_values["error_occurred"] = error_occurred
    if error_message is not None:
        update_values["error_message"] = error_message
    if processing_time_ms is not None:
        update_values["processing_time_ms"] = processing_time_ms
    if request_id is not None:
        update_values["request_id"] = request_id

    if not update_values:
        return

    if "updated_at" in {col.key for col in UserInteraction.__table__.columns}:
        update_values.setdefault("updated_at", dt.datetime.now(UTC))

    await session.execute(
        update(UserInteraction).where(UserInteraction.id == interaction_id).values(**update_values)
    )


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------


async def insert_summary(
    session: AsyncSession,
    *,
    request_id: int,
    lang: str | None = None,
    json_payload: JSONValue = None,
    insights_json: JSONValue = None,
    version: int = 1,
    is_read: bool = False,
) -> int:
    summary = Summary(
        request_id=request_id,
        lang=lang,
        json_payload=prepare_json_payload(json_payload),
        insights_json=prepare_json_payload(insights_json),
        version=version,
        is_read=is_read,
    )
    session.add(summary)
    await session.flush()
    return int(summary.id)


async def upsert_summary(
    session: AsyncSession,
    *,
    request_id: int,
    lang: str | None = None,
    json_payload: JSONValue = None,
    insights_json: JSONValue = None,
    is_read: bool | None = None,
) -> int:
    payload_value = prepare_json_payload(json_payload)
    insights_value = prepare_json_payload(insights_json)

    update_values: dict[str, Any] = {
        "lang": lang,
        "json_payload": payload_value,
        "version": Summary.version + 1,
        "created_at": dt.datetime.now(UTC),
    }
    if insights_value is not None:
        update_values["insights_json"] = insights_value
    if is_read is not None:
        update_values["is_read"] = is_read

    insert_values = {
        "request_id": request_id,
        "lang": lang,
        "json_payload": payload_value,
        "insights_json": insights_value,
        "version": 1,
        "is_read": is_read if is_read is not None else False,
    }
    stmt = (
        pg_insert(Summary)
        .values(**insert_values)
        .on_conflict_do_update(
            index_elements=[Summary.request_id],
            set_=update_values,
        )
        .returning(Summary.version)
    )
    result = await session.execute(stmt)
    version = result.scalar()
    return int(version) if version is not None else 0


async def get_summary_by_request(session: AsyncSession, request_id: int) -> dict[str, Any] | None:
    summary = await session.scalar(select(Summary).where(Summary.request_id == request_id))
    data = model_to_dict(summary)
    if data:
        _convert_bool_fields(data, ["is_read"])
    return data


async def get_read_status(session: AsyncSession, request_id: int) -> bool:
    summary = await session.scalar(select(Summary).where(Summary.request_id == request_id))
    return bool(summary.is_read) if summary else False


async def mark_summary_as_read(session: AsyncSession, request_id: int) -> None:
    await session.execute(
        update(Summary).where(Summary.request_id == request_id).values(is_read=True)
    )


async def get_unread_summaries(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    chat_id: int | None = None,
    limit: int = 10,
    topic: str | None = None,
) -> list[dict[str, Any]]:
    """Return unread summary rows filtered by owner/chat/topic constraints."""
    from app.application.services.topic_search_utils import ensure_mapping, summary_matches_topic

    if limit <= 0:
        return []

    topic_query = topic.strip() if topic else None
    stmt = (
        select(Summary, Request)
        .join(Request, Summary.request_id == Request.id)
        .where(Summary.is_read.is_(False))
        .order_by(Summary.created_at.asc())
    )
    if user_id is not None:
        stmt = stmt.where((Request.user_id == user_id) | (Request.user_id.is_(None)))
    if chat_id is not None:
        stmt = stmt.where((Request.chat_id == chat_id) | (Request.chat_id.is_(None)))

    fetch_limit: int | None = limit
    if topic_query:
        fetch_limit = None

    if fetch_limit is not None:
        stmt = stmt.limit(fetch_limit)

    results: list[dict[str, Any]] = []
    rows = await session.execute(stmt)
    for summary, request in rows:
        payload = ensure_mapping(summary.json_payload)
        request_data = model_to_dict(request) or {}

        if topic_query and not summary_matches_topic(payload, request_data, topic_query):
            continue

        data = model_to_dict(summary) or {}
        req_data = dict(request_data)
        req_data.pop("id", None)
        data.update(req_data)
        if "request_id" not in data:
            data["request_id"] = summary.request_id
        _convert_bool_fields(data, ["is_read"])
        results.append(data)
        if len(results) >= limit:
            break
    return results


async def get_unread_summary_by_request_id(
    session: AsyncSession, request_id: int
) -> dict[str, Any] | None:
    """Return a specific unread summary by request ID."""
    row = (
        await session.execute(
            select(Summary, Request)
            .join(Request, Summary.request_id == Request.id)
            .where(Summary.request_id == request_id, Summary.is_read.is_(False))
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    summary, request = row
    data = model_to_dict(summary) or {}
    req_data = model_to_dict(request) or {}
    req_data.pop("id", None)
    data.update(req_data)
    if "request_id" not in data:
        data["request_id"] = summary.request_id
    _convert_bool_fields(data, ["is_read"])
    return data


async def get_user_interactions(
    session: AsyncSession, *, uid: int, limit: int = 10
) -> list[dict[str, Any]]:
    """Return recent user interactions for a user."""
    rows = (
        await session.scalars(
            select(UserInteraction)
            .where(UserInteraction.user_id == uid)
            .order_by(UserInteraction.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [model_to_dict(row) for row in rows if model_to_dict(row) is not None]


async def insert_audit_log(
    session: AsyncSession,
    *,
    level: str,
    event: str,
    details_json: JSONValue = None,
) -> int:
    entry = AuditLog(
        level=level,
        event=event,
        details_json=prepare_json_payload(details_json),
    )
    session.add(entry)
    await session.flush()
    return int(entry.id)
