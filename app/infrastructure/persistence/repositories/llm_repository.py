"""SQLAlchemy implementation of the LLM-call repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import desc, func, or_, select

if TYPE_CHECKING:
    from app.application.ports.requests import LLMCallRecord
    from app.db.session import Database

from app.db.json_utils import prepare_json_payload
from app.db.models import LLMAttemptTrigger, LLMCall, Request, model_to_dict


def _build_llm_call_payload(call_data: dict[str, Any] | Any) -> dict[str, Any]:
    """Normalize LLM call payloads for single and batched inserts."""
    provider = call_data.get("provider")
    response_text = call_data.get("response_text")

    headers_payload = prepare_json_payload(call_data.get("request_headers_json"), default={})
    messages_payload = prepare_json_payload(call_data.get("request_messages_json"), default=[])
    response_payload = prepare_json_payload(call_data.get("response_json"), default={})
    error_context_payload = prepare_json_payload(call_data.get("error_context_json"))

    payload = {
        "request_id": call_data.get("request_id"),
        "provider": provider,
        "model": call_data.get("model"),
        "endpoint": call_data.get("endpoint"),
        "request_headers_json": headers_payload,
        "request_messages_json": messages_payload,
        "tokens_prompt": call_data.get("tokens_prompt"),
        "tokens_completion": call_data.get("tokens_completion"),
        "cost_usd": call_data.get("cost_usd"),
        "latency_ms": call_data.get("latency_ms"),
        "fallback_model_used": call_data.get("fallback_model_used"),
        "retry_exhausted": bool(call_data.get("retry_exhausted", False)),
        "total_latency_ms": call_data.get("total_latency_ms"),
        "status": call_data.get("status"),
        "error_text": call_data.get("error_text"),
        "structured_output_used": call_data.get("structured_output_used"),
        "structured_output_mode": call_data.get("structured_output_mode"),
        "error_context_json": error_context_payload,
    }

    # Attempt-tracking: pass through if present; the repo layer fills in
    # attempt_index automatically when it is None.
    attempt_trigger = call_data.get("attempt_trigger")
    if attempt_trigger is not None:
        payload["attempt_trigger"] = attempt_trigger

    if provider == "openrouter":
        payload["openrouter_response_text"] = response_text
        payload["openrouter_response_json"] = response_payload
        payload["response_text"] = None
        payload["response_json"] = None
    else:
        payload["response_text"] = response_text
        payload["response_json"] = response_payload

    return payload


async def _compute_next_attempt_index(session: Any, request_id: int | None) -> int:
    """Return max(attempt_index) + 1 for *request_id* within the current transaction.

    Falls back to 1 when request_id is None or no prior rows exist.
    """
    if request_id is None:
        return 1
    current_max = await session.scalar(
        select(func.max(LLMCall.attempt_index)).where(LLMCall.request_id == request_id)
    )
    return (current_max or 0) + 1


async def _resolve_initial_trigger(session: Any, request_id: int | None) -> str | None:
    """Return the ``initial_attempt_trigger`` stored on the parent request.

    Used to inherit the trigger value for the first LLM call of a cloned
    request (e.g. user-initiated retry) without requiring every call site to
    pass the trigger explicitly.  Returns ``None`` when the column is unset.
    """
    if request_id is None:
        return None
    return cast(
        "str | None",
        await session.scalar(
            select(Request.initial_attempt_trigger).where(Request.id == request_id)
        ),
    )


class LLMRepositoryAdapter:
    """Adapter for LLM call logging operations."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_insert_llm_call(self, record: LLMCallRecord) -> int:
        """Insert an LLM call log record.

        ``attempt_index`` is auto-computed as max(attempt_index)+1 for the
        same ``request_id`` when not provided in *record*.

        ``attempt_trigger`` defaults to the ``initial_attempt_trigger`` field
        stored on the parent ``Request`` row (set by retry flows) when this is
        the first call (attempt_index == 1) and no explicit trigger is given.
        Agent-originated calls (``request_id`` is None) are tagged ``"agent"``;
        otherwise it falls back to the model default ``"initial"``.
        """
        async with self._database.transaction() as session:
            payload = _build_llm_call_payload(record)
            req_id: int | None = payload.get("request_id")
            if "attempt_index" not in payload or payload.get("attempt_index") is None:
                payload["attempt_index"] = await _compute_next_attempt_index(session, req_id)
            # For the first call, inherit trigger from the parent request when
            # no explicit trigger was supplied by the caller.
            if payload.get("attempt_trigger") is None and payload.get("attempt_index") == 1:
                inherited = await _resolve_initial_trigger(session, req_id)
                if inherited:
                    payload["attempt_trigger"] = inherited
            # Agent-originated calls have no parent request; tag them explicitly
            # so they persist (request_id is nullable since migration 0051) and
            # stay queryable instead of masquerading as summarize-path "initial".
            if req_id is None and payload.get("attempt_trigger") is None:
                payload["attempt_trigger"] = LLMAttemptTrigger.agent.value
            call = LLMCall(**payload)
            session.add(call)
            await session.flush()
            return call.id

    async def async_insert_llm_calls_batch(
        self,
        calls: list[dict[str, Any]],
    ) -> list[int]:
        """Insert multiple LLM calls in a single transaction.

        ``attempt_index`` is auto-computed per row when not provided.
        """
        if not calls:
            return []

        async with self._database.transaction() as session:
            # Track the running max per request_id so that rows within the
            # same batch are numbered correctly without extra round-trips.
            running_max: dict[int | None, int] = {}
            rows: list[LLMCall] = []
            for call_data in calls:
                payload = _build_llm_call_payload(call_data)
                if "attempt_index" not in payload or payload.get("attempt_index") is None:
                    req_id: int | None = payload.get("request_id")
                    if req_id not in running_max:
                        running_max[req_id] = await _compute_next_attempt_index(session, req_id)
                    else:
                        running_max[req_id] += 1
                    payload["attempt_index"] = running_max[req_id]
                else:
                    # Caller provided explicit value; keep running_max in sync.
                    req_id = payload.get("request_id")
                    explicit = int(payload["attempt_index"])
                    running_max[req_id] = max(running_max.get(req_id, 0), explicit)
                rows.append(LLMCall(**payload))
            session.add_all(rows)
            await session.flush()
            return [row.id for row in rows]

    async def async_get_latest_llm_model_by_request_id(self, request_id: int) -> str | None:
        """Get the latest LLM model used for a request."""
        async with self._database.session() as session:
            return await session.scalar(
                select(LLMCall.model)
                .where(LLMCall.request_id == request_id, LLMCall.model.is_not(None))
                .order_by(LLMCall.id.desc())
                .limit(1)
            )

    async def async_get_llm_calls_by_request(self, request_id: int) -> list[dict[str, Any]]:
        """Get all LLM calls for a request."""
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(LLMCall).where(LLMCall.request_id == request_id).order_by(LLMCall.id)
                )
            ).scalars()
            return [model_to_dict(row) or {} for row in rows]

    async def async_count_llm_calls_by_request(self, request_id: int) -> int:
        """Count LLM calls for a request."""
        async with self._database.session() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(LLMCall)
                    .where(LLMCall.request_id == request_id)
                )
                or 0
            )

    async def async_get_latest_error_by_request(self, request_id: int) -> dict[str, Any] | None:
        """Return the newest error-like LLM call for the request."""
        async with self._database.session() as session:
            row = await session.scalar(
                select(LLMCall)
                .where(
                    LLMCall.request_id == request_id,
                    or_(
                        LLMCall.status == "error",
                        LLMCall.error_text.is_not(None),
                        LLMCall.error_context_json.is_not(None),
                    ),
                )
                .order_by(desc(LLMCall.updated_at), desc(LLMCall.id))
                .limit(1)
            )
            return model_to_dict(row)

    async def async_get_cost_usd_since(self, since: Any) -> float:
        """Return summed LLM cost since the supplied timestamp."""
        async with self._database.session() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(
                    LLMCall.created_at >= since
                )
            )
        return float(total or 0.0)

    async def async_get_max_server_version(self, user_id: int) -> int | None:
        """Return the maximum server_version across LLM calls owned by *user_id*."""
        async with self._database.session() as session:
            value = await session.scalar(
                select(func.max(LLMCall.server_version))
                .join(Request, LLMCall.request_id == Request.id)
                .where(Request.user_id == user_id)
            )
            return int(value) if value is not None else None

    async def async_get_all_for_user(self, user_id: int, *, since: int = 0) -> list[dict[str, Any]]:
        """Get all LLM calls for a user, with request_id flattened.

        ``since`` pushes the sync cursor into the query so a poll only reads rows
        changed past it, instead of the user's entire lifetime history (audit #2).
        """
        stmt = (
            select(LLMCall)
            .join(Request, LLMCall.request_id == Request.id)
            .where(Request.user_id == user_id)
        )
        if since > 0:
            stmt = stmt.where(LLMCall.server_version > since)
        stmt = stmt.order_by(LLMCall.id)
        async with self._database.session() as session:
            rows = (await session.execute(stmt)).scalars()
            return [model_to_dict(row) or {} for row in rows]
