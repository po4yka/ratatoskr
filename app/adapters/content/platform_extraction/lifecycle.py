"""Shared request lifecycle helpers for platform extractors."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )

from app.core.async_utils import raise_if_cancelled
from app.core.validation import safe_telegram_chat_id, safe_telegram_user_id

logger = get_logger(__name__)


class PlatformRequestLifecycle:
    """Shared request creation, notification, and language persistence helpers."""

    def __init__(
        self,
        *,
        response_formatter: ResponseFormatter,
        message_persistence: Any,
        audit_func: Any,
        route_version: int,
    ) -> None:
        self._response_formatter = response_formatter
        self._message_persistence = message_persistence
        self._audit = audit_func
        self._route_version = route_version

    async def send_accepted_notification(
        self,
        request: Any,
    ) -> None:
        if request.mode != "interactive" or request.message is None:
            return
        await self._response_formatter.send_url_accepted_notification(
            request.message,
            request.normalized_url,
            request.correlation_id,
            silent=request.silent,
        )

    async def handle_request_dedupe_or_create(
        self,
        request: Any,
        *,
        dedupe_hash: str,
        paper_canonical_id: str | None = None,
    ) -> int:
        if request.mode != "interactive" or request.message is None:
            msg = "Interactive request lifecycle requires a Telegram message"
            raise RuntimeError(msg)

        await self._upsert_sender_metadata(request.message)
        try:
            req_id = await self.create_request(
                request=request,
                dedupe_hash=dedupe_hash,
                paper_canonical_id=paper_canonical_id,
            )
            self._audit(
                "INFO",
                "url_request_created",
                {
                    "request_id": req_id,
                    "hash": dedupe_hash,
                    "paper_canonical_id": paper_canonical_id,
                    "url": request.url_text,
                    "cid": request.correlation_id,
                },
            )
            return req_id
        except Exception as create_error:
            # Look up by paper_canonical_id first if it's set — the academic
            # path uses it as the authoritative dedupe key. Fall back to
            # dedupe_hash for non-academic / no-paper-id cases.
            existing_req = None
            if paper_canonical_id:
                existing_req = await self._message_persistence.request_repo.async_get_request_by_paper_canonical_id(
                    paper_canonical_id
                )
            if existing_req is None:
                existing_req = (
                    await self._message_persistence.request_repo.async_get_request_by_dedupe_hash(
                        dedupe_hash
                    )
                )
            if existing_req:
                req_id = int(existing_req["id"])
                if request.correlation_id:
                    try:
                        await self._message_persistence.request_repo.async_update_request_correlation_id(
                            req_id,
                            request.correlation_id,
                        )
                    except Exception as exc:
                        logger.debug(
                            "correlation_id_update_failed",
                            extra={"cid": request.correlation_id, "error": str(exc)},
                        )
                return req_id
            raise create_error

    async def create_request(
        self,
        *,
        request: Any,
        dedupe_hash: str,
        paper_canonical_id: str | None = None,
    ) -> int:
        req_id = await self._message_persistence.request_repo.async_create_request(
            type_="url",
            status="pending",
            correlation_id=request.correlation_id,
            chat_id=request.chat_id,
            user_id=request.user_id,
            input_url=request.url_text,
            normalized_url=request.normalized_url,
            dedupe_hash=dedupe_hash,
            paper_canonical_id=paper_canonical_id,
            input_message_id=request.message_id,
            route_version=self._route_version,
        )

        if request.message is not None:
            try:
                await self._message_persistence.persist_message_snapshot(req_id, request.message)
            except Exception as exc:
                raise_if_cancelled(exc)
                logger.error(
                    "snapshot_error",
                    extra={"error": str(exc), "cid": request.correlation_id},
                )
        return cast("int", req_id)

    async def persist_detected_lang(self, request_id: int, detected_lang: str) -> None:
        try:
            await self._message_persistence.request_repo.async_update_request_lang_detected(
                request_id,
                detected_lang,
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.error(
                "persist_lang_detected_error",
                extra={"error": str(exc), "request_id": request_id},
            )

    async def _upsert_sender_metadata(self, message: Any) -> None:
        chat_obj = getattr(message, "chat", None)
        chat_id = safe_telegram_chat_id(
            getattr(chat_obj, "id", None) if chat_obj is not None else None,
            field_name="chat_id",
        )
        if chat_id is not None:
            try:
                await self._message_persistence.user_repo.async_upsert_chat(
                    chat_id=chat_id,
                    type_=str(getattr(chat_obj, "type", None))
                    if getattr(chat_obj, "type", None) is not None
                    else None,
                    title=str(getattr(chat_obj, "title", None))
                    if isinstance(getattr(chat_obj, "title", None), str)
                    else None,
                    username=str(getattr(chat_obj, "username", None))
                    if isinstance(getattr(chat_obj, "username", None), str)
                    else None,
                )
            except Exception as exc:
                logger.warning(
                    "chat_upsert_failed",
                    extra={"chat_id": chat_id, "error": str(exc)},
                )

        from_user_obj = getattr(message, "from_user", None)
        user_id = safe_telegram_user_id(
            getattr(from_user_obj, "id", None) if from_user_obj is not None else None,
            field_name="user_id",
        )
        if user_id is not None:
            try:
                await self._message_persistence.user_repo.async_upsert_user(
                    telegram_user_id=user_id,
                    username=str(getattr(from_user_obj, "username", None))
                    if isinstance(getattr(from_user_obj, "username", None), str)
                    else None,
                )
            except Exception as exc:
                logger.warning(
                    "user_upsert_failed",
                    extra={"user_id": user_id, "error": str(exc)},
                )
