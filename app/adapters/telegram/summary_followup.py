"""Follow-up Q&A state + grounded answer generation for summaries."""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from typing import TYPE_CHECKING, Any, TypedDict

from app.adapters.external.formatting.markdown_telegram import render_markdown
from app.core.logging_utils import get_logger
from app.core.ui_strings import t
from app.infrastructure.persistence.repositories.crawl_result_repository import (
    CrawlResultRepositoryAdapter,
)

logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.db.session import Database


class _FollowupSession(TypedDict):
    summary_id: str
    history: list[dict[str, str]]
    started_at: float
    updated_at: float


_FOLLOWUP_TTL_SECONDS = 20 * 60
_FOLLOWUP_MAX_HISTORY_PAIRS = 4
_FOLLOWUP_MAX_QUESTION_CHARS = 1200
_FOLLOWUP_LLM_TIMEOUT_SEC = 120.0
_FOLLOWUP_MAX_SOURCE_CHARS = 12000
_FOLLOWUP_MAX_SUMMARY_JSON_CHARS = 8000


class SummaryFollowupManager:
    """Manages per-user follow-up sessions and grounded Q&A generation."""

    def __init__(
        self,
        *,
        db: Database,
        response_formatter: ResponseFormatter,
        url_handler: Any | None,
        lang: str,
        load_summary_payload: Callable[..., Awaitable[dict[str, Any] | None]],
    ) -> None:
        self._crawl_result_repo = CrawlResultRepositoryAdapter(db)
        self._response_formatter = response_formatter
        self._url_handler = url_handler
        self._lang = lang
        self._load_summary_payload = load_summary_payload
        self._sessions: dict[int, _FollowupSession] = {}
        self._lock = asyncio.Lock()

    async def has_pending(self, uid: int) -> bool:
        await self._cleanup_expired()
        async with self._lock:
            return uid in self._sessions

    async def clear(self, uid: int) -> None:
        async with self._lock:
            self._sessions.pop(uid, None)

    async def activate(self, uid: int, summary_id: str) -> None:
        now = time.time()
        async with self._lock:
            self._sessions[uid] = {
                "summary_id": summary_id,
                "history": [],
                "started_at": now,
                "updated_at": now,
            }

    async def start_session(
        self,
        *,
        message: Any,
        uid: int,
        summary_id: str,
        correlation_id: str,
    ) -> None:
        summary_data = await self._load_summary_payload(summary_id, correlation_id=correlation_id)
        if not summary_data:
            await self._response_formatter.safe_reply(
                message, t("cb_summary_not_found", self._lang)
            )
            return

        await self.activate(uid, summary_id)
        await self._response_formatter.safe_reply(
            message,
            f"{t('cb_followup_prompt', self._lang)}\n\n{t('cb_followup_continue', self._lang)}",
        )
        logger.info(
            "followup_session_started",
            extra={"uid": uid, "summary_id": summary_id, "cid": correlation_id},
        )

    async def answer(
        self,
        *,
        message: Any,
        uid: int,
        question: str,
        correlation_id: str,
    ) -> bool:
        question_clean = question.strip()
        if not question_clean:
            return False
        if len(question_clean) > _FOLLOWUP_MAX_QUESTION_CHARS:
            question_clean = question_clean[:_FOLLOWUP_MAX_QUESTION_CHARS].rstrip() + "…"

        session = await self._get_session(uid)
        if not session:
            return False

        summary_id = session["summary_id"]
        summary_data = await self._load_summary_payload(summary_id, correlation_id=correlation_id)
        if not summary_data:
            await self.clear(uid)
            await self._response_formatter.safe_reply(
                message, t("cb_summary_not_found", self._lang)
            )
            return True

        llm_client = self._resolve_llm_client()
        if llm_client is None:
            await self._response_formatter.safe_reply(
                message, t("cb_followup_unavailable", self._lang)
            )
            return True

        await self._response_formatter.safe_reply(message, t("cb_followup_thinking", self._lang))

        source_context = await asyncio.to_thread(
            self._load_source_context,
            summary_data.get("request_id"),
            correlation_id=correlation_id,
        )
        try:
            answer_text = await asyncio.wait_for(
                self._generate_answer(
                    llm_client=llm_client,
                    summary_data=summary_data,
                    source_context=source_context,
                    question=question_clean,
                    history=session.get("history") or [],
                    correlation_id=correlation_id,
                ),
                timeout=_FOLLOWUP_LLM_TIMEOUT_SEC,
            )
        except TimeoutError:
            logger.warning(
                "followup_llm_timeout",
                extra={"uid": uid, "summary_id": summary_id, "cid": correlation_id},
            )
            await self._response_formatter.safe_reply(message, t("cb_timeout", self._lang))
            return True

        if not answer_text:
            answer_text = t("cb_followup_no_answer", self._lang)

        # The LLM answer is Markdown prose; render it to Telegram HTML so bold,
        # lists, links and quotes display. History keeps the raw answer.
        rendered_answer = render_markdown(answer_text)
        continue_hint = html.escape(t("cb_followup_continue", self._lang))
        await self._response_formatter.safe_reply(
            message,
            f"{rendered_answer}\n\n{continue_hint}",
            parse_mode="HTML",
        )
        await self._append_history(
            uid=uid,
            summary_id=summary_id,
            question=question_clean,
            answer=answer_text,
        )
        return True

    async def _get_session(self, uid: int) -> _FollowupSession | None:
        await self._cleanup_expired()
        async with self._lock:
            session = self._sessions.get(uid)
            if session is None:
                return None
            return {
                "summary_id": session["summary_id"],
                "history": list(session.get("history") or []),
                "started_at": session.get("started_at", 0.0),
                "updated_at": session.get("updated_at", 0.0),
            }

    async def _append_history(
        self,
        *,
        uid: int,
        summary_id: str,
        question: str,
        answer: str,
    ) -> None:
        now = time.time()
        async with self._lock:
            session = self._sessions.get(uid)
            if not session or session.get("summary_id") != summary_id:
                return
            history = list(session.get("history") or [])
            history.append({"question": question, "answer": answer})
            session["history"] = history[-_FOLLOWUP_MAX_HISTORY_PAIRS:]
            session["updated_at"] = now

    async def _cleanup_expired(self) -> None:
        now = time.time()
        async with self._lock:
            expired_uids = [
                uid
                for uid, session in self._sessions.items()
                if now - float(session.get("updated_at", 0.0)) > _FOLLOWUP_TTL_SECONDS
            ]
            for uid in expired_uids:
                self._sessions.pop(uid, None)

    @staticmethod
    def _truncate_for_prompt(text: str, *, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "\n...[truncated]"

    @staticmethod
    def _normalize_source_text(html_blob: str) -> str:
        without_scripts = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", html_blob)
        without_tags = re.sub(r"(?s)<[^>]+>", " ", without_scripts)
        plain = html.unescape(without_tags)
        return re.sub(r"\\s+", " ", plain).strip()

    def _load_source_context(
        self,
        request_id: Any,
        *,
        correlation_id: str | None = None,
    ) -> str:
        if not isinstance(request_id, int):
            return ""
        try:
            crawl = asyncio.run(
                self._crawl_result_repo.async_get_crawl_result_by_request(request_id)
            )
            if not crawl:
                return ""

            markdown = str(crawl.get("content_markdown") or "").strip()
            if markdown:
                return self._truncate_for_prompt(markdown, max_chars=_FOLLOWUP_MAX_SOURCE_CHARS)

            content_html = str(crawl.get("content_html") or "").strip()
            if content_html:
                plain_html = self._normalize_source_text(content_html)
                if plain_html:
                    return self._truncate_for_prompt(
                        plain_html, max_chars=_FOLLOWUP_MAX_SOURCE_CHARS
                    )

            structured = crawl.get("structured_json")
            if isinstance(structured, (dict, list)):
                structured_text = json.dumps(structured, ensure_ascii=False, sort_keys=True)
                return self._truncate_for_prompt(
                    structured_text, max_chars=_FOLLOWUP_MAX_SOURCE_CHARS
                )
        except Exception as e:
            logger.warning(
                "load_followup_source_context_failed",
                extra={"request_id": request_id, "error": str(e), "cid": correlation_id},
            )
        return ""

    def _resolve_llm_client(self) -> Any | None:
        handler = self._url_handler
        if handler is None:
            return None

        llm_client = getattr(handler, "_llm_client", None)
        if llm_client is not None and callable(getattr(llm_client, "chat", None)):
            return llm_client
        return None

    def _build_messages(
        self,
        *,
        summary_json: str,
        source_context: str,
        history: list[dict[str, str]],
        question: str,
    ) -> list[dict[str, str]]:
        history_lines: list[str] = []
        for i, pair in enumerate(history[-_FOLLOWUP_MAX_HISTORY_PAIRS:], 1):
            q = str(pair.get("question") or "").strip()
            a = str(pair.get("answer") or "").strip()
            if not q and not a:
                continue
            if q:
                history_lines.append(f"{i}. Q: {q}")
            if a:
                history_lines.append(f"{i}. A: {a}")

        history_block = "\n".join(history_lines).strip() or "(none)"
        source_block = source_context or "(no stored source excerpt available)"
        user_prompt = (
            "Stored summary (JSON):\n"
            f"{summary_json}\n\n"
            "Stored source excerpt:\n"
            f"{source_block}\n\n"
            "Follow-up history:\n"
            f"{history_block}\n\n"
            "User question:\n"
            f"{question}"
        )

        return [
            {
                "role": "system",
                "content": (
                    "You answer follow-up questions only from the provided stored summary and source "
                    "excerpt. Do not use external knowledge. If evidence is insufficient, say so "
                    "explicitly. Keep the answer concise and include a short 'Evidence:' section "
                    "with supporting points from the provided context."
                ),
            },
            {"role": "user", "content": user_prompt},
        ]

    async def _generate_answer(
        self,
        *,
        llm_client: Any,
        summary_data: dict[str, Any],
        source_context: str,
        question: str,
        history: list[dict[str, str]],
        correlation_id: str,
    ) -> str | None:
        summary_context = {
            "summary_id": summary_data.get("id"),
            "request_id": summary_data.get("request_id"),
            "url": summary_data.get("url"),
            "lang": summary_data.get("lang"),
            "summary_250": summary_data.get("summary_250"),
            "summary_1000": summary_data.get("summary_1000"),
            "tldr": summary_data.get("tldr"),
            "key_ideas": summary_data.get("key_ideas"),
            "topic_tags": summary_data.get("topic_tags"),
            "entities": summary_data.get("entities"),
            "answered_questions": summary_data.get("answered_questions"),
            "metadata": summary_data.get("metadata"),
            "confidence": summary_data.get("confidence"),
            "hallucination_risk": summary_data.get("hallucination_risk"),
        }
        summary_json = json.dumps(summary_context, ensure_ascii=False, sort_keys=True, indent=2)
        summary_json = self._truncate_for_prompt(
            summary_json, max_chars=_FOLLOWUP_MAX_SUMMARY_JSON_CHARS
        )
        source_text = self._truncate_for_prompt(
            source_context, max_chars=_FOLLOWUP_MAX_SOURCE_CHARS
        )

        messages = self._build_messages(
            summary_json=summary_json,
            source_context=source_text,
            history=history,
            question=question,
        )
        request_id = (
            summary_data.get("request_id")
            if isinstance(summary_data.get("request_id"), int)
            else None
        )

        try:
            llm_result = await llm_client.chat(
                messages,
                temperature=0.1,
                max_tokens=700,
                request_id=request_id,
            )
            answer_text = str(getattr(llm_result, "response_text", "") or "").strip()
            if answer_text:
                return answer_text
            logger.warning(
                "followup_llm_empty_response",
                extra={
                    "status": getattr(llm_result, "status", None),
                    "request_id": request_id,
                    "cid": correlation_id,
                },
            )
        except Exception as e:
            logger.exception(
                "followup_llm_failed",
                extra={"request_id": request_id, "error": str(e), "cid": correlation_id},
            )
        return None
