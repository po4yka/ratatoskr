from __future__ import annotations

from typing import Any

from app.adapters.content.streaming import StreamEvent, get_stream_hub
from app.core.json_utils import dumps as json_dumps


class BackgroundProgressPublisher:
    def __init__(
        self,
        *,
        redis: Any | None,
        logger: Any,
        progress_event_repo: Any | None = None,
    ) -> None:
        self._redis = redis
        self._logger = logger
        self._progress_event_repo = progress_event_repo

    async def publish(
        self,
        request_id: int,
        status: str,
        stage: str,
        message: str,
        progress: float,
        error: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        kind = _event_kind(status)
        normalized_stage = _normalize_stage(stage)
        normalized_status = _normalize_status(status)
        payload = {
            "request_id": request_id,
            "status": normalized_status,
            "stage": normalized_stage,
            "message": message,
            "progress": progress,
            "error": error,
        }
        event_payload = payload if error is None else {**payload, "error_message": error}
        persisted = None
        if self._progress_event_repo is not None:
            try:
                persisted = await self._progress_event_repo.append(
                    request_id=request_id,
                    kind=kind,
                    stage=normalized_stage,
                    status=normalized_status,
                    message=message,
                    progress=progress,
                    payload=event_payload,
                    correlation_id=correlation_id,
                )
            except Exception as exc:
                self._logger.warning(
                    "bg_progress_event_persist_failed",
                    exc_info=True,
                    extra={"request_id": request_id, "error": str(exc)},
                )
        self._publish_local(request_id, persisted, kind, event_payload)

        if not self._redis:
            return

        wire_payload = persisted.as_sse_payload() if persisted is not None else event_payload
        channel = f"processing:request:{request_id}"
        try:
            await self._redis.publish(channel, json_dumps(wire_payload))
        except Exception as exc:
            self._logger.warning(
                "bg_redis_publish_failed",
                exc_info=True,
                extra={"channel": channel, "error": str(exc)},
            )

    def _publish_local(
        self,
        request_id: int,
        persisted: Any | None,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        try:
            event_payload = persisted.as_sse_payload() if persisted is not None else payload
            correlation_id = event_payload.get("correlation_id") or ""
            stream_payload: dict[str, Any]
            if kind == "done":
                stream_payload = {"summary_id": None, "request_id": str(request_id)}
            elif kind == "error":
                stream_payload = {
                    "code": str(event_payload.get("status") or "failed").upper(),
                    "message": str(
                        event_payload.get("message") or event_payload.get("error") or ""
                    ),
                    "correlation_id": correlation_id,
                }
            else:
                stream_payload = {"stage": event_payload.get("stage") or "queued"}
            get_stream_hub().publish(
                str(request_id),
                StreamEvent.now(kind, stream_payload, correlation_id),
            )
        except Exception as exc:
            self._logger.debug(
                "bg_local_stream_publish_failed",
                extra={"request_id": request_id, "error": str(exc)},
            )


def _event_kind(status: str) -> str:
    normalized = status.lower()
    if normalized in {"completed", "complete", "success", "succeeded"}:
        return "done"
    if normalized in {"failed", "error", "cancelled"}:
        return "error"
    return "stage"


def _normalize_status(status: str) -> str:
    normalized = status.lower()
    if normalized in {"processing", "running"}:
        return "running"
    if normalized in {"completed", "complete", "success", "succeeded"}:
        return "succeeded"
    if normalized in {"failed", "error"}:
        return "failed"
    if normalized == "cancelled":
        return "cancelled"
    return "pending" if normalized in {"queued", "pending"} else normalized


def _normalize_stage(stage: str) -> str:
    normalized = stage.lower()
    return {
        "queued": "queued",
        "extraction": "extracting",
        "extracting": "extracting",
        "summarization": "summarizing",
        "summarizing": "summarizing",
        "validation": "validating",
        "validating": "validating",
        "saving": "persisting",
        "persisting": "persisting",
        "done": "done",
        "unknown": "done",
        "cancelled": "done",
    }.get(normalized, normalized)
