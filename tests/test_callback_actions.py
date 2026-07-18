from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.adapters.telegram.callback_actions as callback_actions_module
from app.adapters.telegram.callback_actions import CallbackActionService
from app.core.ui_strings import t

if TYPE_CHECKING:
    from pathlib import Path


class _ResponseFormatterStub:
    def __init__(self) -> None:
        self.safe_reply = AsyncMock()
        self.send_error_notification = AsyncMock()
        self.send_topic_search_results = AsyncMock()
        self.send_russian_translation = AsyncMock()


def _make_service(
    *,
    url_handler: Any | None = None,
    hybrid_search: Any | None = None,
) -> tuple[CallbackActionService, _ResponseFormatterStub]:
    formatter = _ResponseFormatterStub()
    service = CallbackActionService(
        db=MagicMock(),
        response_formatter=cast("Any", formatter),
        url_handler=cast("Any", url_handler),
        hybrid_search=cast("Any", hybrid_search),
        lang="en",
    )
    return service, formatter


def _last_reply_text(formatter: _ResponseFormatterStub) -> str:
    return str(formatter.safe_reply.await_args.args[1])


def _set_load_summary_payload(
    service: CallbackActionService,
    payload: dict[str, Any] | None,
) -> None:
    cast("Any", service).load_summary_payload = AsyncMock(return_value=payload)


async def _timeout_wait_for(coro: Any, timeout: float) -> Any:
    if hasattr(coro, "close"):
        coro.close()
    raise TimeoutError


class TestFindSimilar:
    @pytest.mark.asyncio
    async def test_prefers_title_then_tags_for_query(self) -> None:
        hybrid_search = SimpleNamespace(
            search=AsyncMock(return_value=[SimpleNamespace(url="other")])
        )
        service, formatter = _make_service(hybrid_search=hybrid_search)
        _set_load_summary_payload(
            service,
            {
                "metadata": {"title": "Example title"},
                "topic_tags": ["#ai", "#ml", "#ignored"],
                "url": "https://example.com/current",
            },
        )

        handled = await service.handle_find_similar(
            SimpleNamespace(),
            uid=5,
            parts=["similar", "42"],
            correlation_id="cid-similar",
        )

        assert handled is True
        hybrid_search.search.assert_awaited_once_with(
            "Example title #ai #ml",
            correlation_id="cid-similar",
        )
        formatter.send_topic_search_results.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_first_key_idea_when_title_and_tags_missing(self) -> None:
        hybrid_search = SimpleNamespace(search=AsyncMock(return_value=[]))
        service, formatter = _make_service(hybrid_search=hybrid_search)
        _set_load_summary_payload(
            service,
            {
                "key_ideas": ["First idea should become the query", "Second"],
                "url": "https://example.com/current",
            },
        )

        handled = await service.handle_find_similar(
            SimpleNamespace(),
            uid=5,
            parts=["similar", "42"],
            correlation_id="cid-idea",
        )

        assert handled is True
        hybrid_search.search.assert_awaited_once_with(
            "First idea should become the query",
            correlation_id="cid-idea",
        )
        assert _last_reply_text(formatter).startswith(t("cb_no_similar", "en"))

    @pytest.mark.asyncio
    async def test_replies_when_not_enough_information_for_query(self) -> None:
        hybrid_search = SimpleNamespace(search=AsyncMock())
        service, formatter = _make_service(hybrid_search=hybrid_search)
        _set_load_summary_payload(service, {"metadata": {}, "topic_tags": []})

        handled = await service.handle_find_similar(
            SimpleNamespace(),
            uid=5,
            parts=["similar", "42"],
            correlation_id="cid-empty",
        )

        assert handled is True
        hybrid_search.search.assert_not_awaited()
        assert _last_reply_text(formatter) == t("cb_not_enough_info", "en")

    @pytest.mark.asyncio
    async def test_filters_current_url_from_results(self) -> None:
        duplicate = SimpleNamespace(url="https://example.com/current", title="dup")
        remaining = SimpleNamespace(url="https://example.com/other", title="other")
        hybrid_search = SimpleNamespace(search=AsyncMock(return_value=[duplicate, remaining]))
        service, formatter = _make_service(hybrid_search=hybrid_search)
        _set_load_summary_payload(
            service,
            {
                "metadata": {"title": "Example title"},
                "topic_tags": [],
                "url": "https://example.com/current",
            },
        )

        handled = await service.handle_find_similar(
            SimpleNamespace(),
            uid=5,
            parts=["similar", "42"],
            correlation_id="cid-filter",
        )

        assert handled is True
        formatter.send_topic_search_results.assert_awaited_once()
        sent_articles = formatter.send_topic_search_results.await_args.kwargs["articles"]
        assert sent_articles == [remaining]

    @pytest.mark.asyncio
    async def test_handles_search_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hybrid_search = SimpleNamespace(search=AsyncMock())
        service, formatter = _make_service(hybrid_search=hybrid_search)
        _set_load_summary_payload(service, {"metadata": {"title": "Example"}})
        monkeypatch.setattr(callback_actions_module.asyncio, "wait_for", _timeout_wait_for)

        handled = await service.handle_find_similar(
            SimpleNamespace(),
            uid=5,
            parts=["similar", "42"],
            correlation_id="cid-timeout",
        )

        assert handled is True
        assert _last_reply_text(formatter) == t("cb_timeout", "en")


class TestMoreDetails:
    @pytest.mark.asyncio
    async def test_renders_html_sections_with_escaping_and_truncation(self) -> None:
        service, formatter = _make_service()
        _set_load_summary_payload(
            service,
            {
                "metadata": {
                    "title": "<Dangerous title>",
                    "domain": "example.com<script>",
                },
                "summary_1000": "Long summary body",
                "insights": {
                    "topic_overview": "A" * 510,
                    "new_facts": [
                        {"fact": "<Fact 1>"},
                        {"fact": "B" * 230},
                    ],
                },
                "answered_questions": ["What happened?<tag>"],
                "topic_tags": ["#alpha", "", "#beta", "#gamma", "#delta", "#epsilon", "#zeta"],
                "entities": {
                    "people": ["Alice", "Bob", "C" * 10],
                    "organizations": ["<Org>"],
                    "locations": ["Tbilisi"],
                },
            },
        )

        handled = await service.handle_more(
            SimpleNamespace(),
            uid=5,
            parts=["more", "42"],
            correlation_id="cid-more",
        )

        assert handled is True
        reply_text = _last_reply_text(formatter)
        assert "<Dangerous title>" not in reply_text
        assert "&lt;Dangerous title&gt;" in reply_text
        assert "example.com&lt;script&gt;" in reply_text
        assert t("more_long_summary", "en") in reply_text
        assert t("more_research_highlights", "en") in reply_text
        assert t("more_answered_questions", "en") in reply_text
        assert t("more_tags", "en") in reply_text
        assert t("more_entities", "en") in reply_text
        assert "(+1)" in reply_text
        assert "…" in reply_text
        assert formatter.safe_reply.await_args.kwargs["parse_mode"] == "HTML"

    @pytest.mark.asyncio
    async def test_falls_back_to_no_details_message(self) -> None:
        service, formatter = _make_service()
        _set_load_summary_payload(service, {"metadata": {}, "topic_tags": []})

        handled = await service.handle_more(
            SimpleNamespace(),
            uid=5,
            parts=["more", "42"],
            correlation_id="cid-empty-more",
        )

        assert handled is True
        assert _last_reply_text(formatter) == t("cb_no_details", "en")


class TestRelatedSummary:
    @pytest.mark.asyncio
    async def test_formats_related_summary_and_attaches_keyboard(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service, formatter = _make_service()
        _set_load_summary_payload(
            service,
            {
                "id": "99",
                "title": "<A title>",
                "tldr": "Short <summary>",
                "key_ideas": ["Idea 1", "Idea 2"],
                "topic_tags": ["#x", "#y"],
                "url": "https://example.com/source",
            },
        )
        keyboard = object()
        monkeypatch.setattr(
            "app.adapters.external.formatting.summary.action_buttons.create_inline_keyboard",
            lambda summary_id, correlation_id=None, lang="en": keyboard,
        )

        handled = await service.handle_show_related_summary(
            SimpleNamespace(),
            uid=5,
            parts=["rel", "10"],
            correlation_id="cid-related",
        )

        assert handled is True
        reply_text = _last_reply_text(formatter)
        assert "&lt;A title&gt;" in reply_text
        assert "Short &lt;summary&gt;" in reply_text
        assert "Idea 1" in reply_text
        assert "Source" in reply_text
        assert formatter.safe_reply.await_args.kwargs["reply_markup"] is keyboard
        assert formatter.safe_reply.await_args.kwargs["parse_mode"] == "HTML"


class TestTranslate:
    @pytest.mark.asyncio
    async def test_not_found_summary(self) -> None:
        service, formatter = _make_service()
        _set_load_summary_payload(service, None)

        handled = await service.handle_translate(
            SimpleNamespace(),
            uid=1,
            parts=["translate", "42"],
            correlation_id="cid-translate",
        )

        assert handled is True
        assert _last_reply_text(formatter) == t("cb_summary_not_found", "en")

    @pytest.mark.asyncio
    async def test_already_russian_summary(self) -> None:
        service, formatter = _make_service()
        _set_load_summary_payload(service, {"lang": "ru"})

        handled = await service.handle_translate(
            SimpleNamespace(),
            uid=1,
            parts=["translate", "42"],
            correlation_id="cid-translate",
        )

        assert handled is True
        assert _last_reply_text(formatter) == t("cb_translation_already_ru", "en")

    @pytest.mark.asyncio
    async def test_unavailable_translation_service(self) -> None:
        service, formatter = _make_service()
        _set_load_summary_payload(service, {"lang": "en", "request_id": 42})

        handled = await service.handle_translate(
            SimpleNamespace(),
            uid=1,
            parts=["translate", "42"],
            correlation_id="cid-translate",
        )

        assert handled is True
        formatter.send_error_notification.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_successful_translation(self) -> None:
        url_handler = SimpleNamespace(
            translate_summary_to_ru=AsyncMock(return_value="Translated text"),
        )
        service, formatter = _make_service(url_handler=url_handler)
        summary_data = {"lang": "en", "request_id": 42, "summary_250": "Short"}
        _set_load_summary_payload(service, summary_data)
        message = SimpleNamespace()

        handled = await service.handle_translate(
            message,
            uid=1,
            parts=["translate", "42"],
            correlation_id="cid-translate",
        )

        assert handled is True
        url_handler.translate_summary_to_ru.assert_awaited_once()
        formatter.send_russian_translation.assert_awaited_once_with(
            message,
            "Translated text",
            correlation_id="cid-translate",
        )

    @pytest.mark.asyncio
    async def test_translation_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url_handler = SimpleNamespace(
            translate_summary_to_ru=AsyncMock(return_value="ignored"),
        )
        service, formatter = _make_service(url_handler=url_handler)
        _set_load_summary_payload(service, {"lang": "en", "request_id": 42})
        monkeypatch.setattr(callback_actions_module.asyncio, "wait_for", _timeout_wait_for)

        handled = await service.handle_translate(
            SimpleNamespace(),
            uid=1,
            parts=["translate", "42"],
            correlation_id="cid-translate-timeout",
        )

        assert handled is True
        assert _last_reply_text(formatter) == t("cb_timeout", "en")


class TestExport:
    @pytest.mark.asyncio
    async def test_rejects_unknown_format(self) -> None:
        service, formatter = _make_service()

        handled = await service.handle_export(
            SimpleNamespace(),
            uid=1,
            parts=["export", "42", "txt"],
            correlation_id="cid-export",
        )

        assert handled is True
        assert _last_reply_text(formatter) == "Unknown export format: txt"

    @pytest.mark.asyncio
    async def test_sends_document_and_cleans_up_temp_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        export_path = tmp_path / "summary.pdf"
        export_path.write_text("pdf data", encoding="utf-8")
        message = SimpleNamespace(reply_document=AsyncMock())
        service, formatter = _make_service()

        def _fake_export_summary(
            self: Any,
            summary_id: str,
            export_format: str,
            correlation_id: str | None = None,
        ) -> tuple[str | None, str | None]:
            return str(export_path), "summary.pdf"

        monkeypatch.setattr(
            "app.adapters.external.formatting.export_formatter.ExportFormatter.export_summary",
            _fake_export_summary,
        )
        monkeypatch.setattr(service._store, "summary_belongs_to_user", AsyncMock(return_value=True))

        handled = await service.handle_export(
            message,
            uid=1,
            parts=["export", "42", "pdf"],
            correlation_id="cid-export",
        )

        assert handled is True
        message.reply_document.assert_awaited_once()
        assert not export_path.exists()
        assert formatter.safe_reply.await_args_list[0].args[1] == t(
            "cb_export_generating", "en"
        ).format(fmt="PDF")

    @pytest.mark.asyncio
    async def test_handles_export_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, formatter = _make_service()
        monkeypatch.setattr(callback_actions_module.asyncio, "wait_for", _timeout_wait_for)
        monkeypatch.setattr(service._store, "summary_belongs_to_user", AsyncMock(return_value=True))

        handled = await service.handle_export(
            SimpleNamespace(),
            uid=1,
            parts=["export", "42", "pdf"],
            correlation_id="cid-export-timeout",
        )

        assert handled is True
        assert _last_reply_text(formatter) == t("cb_timeout", "en")

    @pytest.mark.asyncio
    async def test_export_denied_for_non_owner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, formatter = _make_service()
        monkeypatch.setattr(
            service._store, "summary_belongs_to_user", AsyncMock(return_value=False)
        )
        export_spy = MagicMock()
        monkeypatch.setattr(
            "app.adapters.external.formatting.export_formatter.ExportFormatter.export_summary",
            export_spy,
        )

        handled = await service.handle_export(
            SimpleNamespace(),
            uid=999,
            parts=["export", "42", "pdf"],
            correlation_id="cid-export-idor",
        )

        assert handled is True
        # Non-owner sees a neutral "not found", and the exporter never runs.
        assert _last_reply_text(formatter) == t("cb_summary_not_found", "en")
        export_spy.assert_not_called()


class TestSaveAndRate:
    @pytest.mark.asyncio
    async def test_save_not_found_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, formatter = _make_service()

        monkeypatch.setattr(service._store, "toggle_save", AsyncMock(return_value=None))

        handled = await service.handle_toggle_save(
            SimpleNamespace(),
            uid=1,
            parts=["save", "42"],
            correlation_id="cid-save",
        )

        assert handled is True
        assert _last_reply_text(formatter) == t("cb_summary_not_found", "en")

    @pytest.mark.asyncio
    async def test_rate_positive_and_negative_feedback(self) -> None:
        service, formatter = _make_service()

        positive = await service.handle_rate(
            SimpleNamespace(),
            uid=1,
            parts=["rate", "42", "1"],
            correlation_id="cid-rate",
        )
        positive_text = _last_reply_text(formatter)

        negative = await service.handle_rate(
            SimpleNamespace(),
            uid=1,
            parts=["rate", "42", "-1"],
            correlation_id="cid-rate",
        )
        negative_text = _last_reply_text(formatter)

        assert positive is True
        assert negative is True
        assert t("cb_feedback_positive", "en") in positive_text
        assert t("cb_feedback_negative", "en") in negative_text

    @pytest.mark.asyncio
    async def test_invalid_rating_returns_false(self) -> None:
        service, formatter = _make_service()

        handled = await service.handle_rate(
            SimpleNamespace(),
            uid=1,
            parts=["rate", "42", "bad"],
            correlation_id="cid-rate",
        )

        assert handled is False
        formatter.safe_reply.assert_not_awaited()


class TestDigestAndRetry:
    @pytest.mark.asyncio
    async def test_digest_falls_back_to_full_post_when_url_processing_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        url_handler = SimpleNamespace(handle_single_url=AsyncMock(side_effect=RuntimeError("boom")))
        service, formatter = _make_service(url_handler=url_handler)
        post = SimpleNamespace(text="Stored post text", url="https://example.com/post")

        async def _fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
            return post

        monkeypatch.setattr(callback_actions_module.asyncio, "to_thread", _fake_to_thread)

        handled = await service.handle_digest_full_summary(
            SimpleNamespace(),
            uid=1,
            parts=["dg", "10", "20"],
            correlation_id="cid-digest",
        )

        assert handled is True
        assert formatter.safe_reply.await_args_list[0].args[1] == t("cb_generating_summary", "en")
        assert "Full Post" in formatter.safe_reply.await_args_list[1].args[1]
        assert "Original" in formatter.safe_reply.await_args_list[1].args[1]

    @pytest.mark.asyncio
    async def test_retry_replies_when_original_url_not_found(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service, formatter = _make_service()

        monkeypatch.setattr(service._store, "lookup_retry_url", AsyncMock(return_value=None))

        handled = await service.handle_retry(
            SimpleNamespace(),
            uid=1,
            parts=["retry", "orig-cid"],
            correlation_id="cid-retry",
        )

        assert handled is True
        assert _last_reply_text(formatter) == t("cb_retry_url_not_found", "en")

    @pytest.mark.asyncio
    async def test_retry_calls_url_handler_when_url_exists(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        url_handler = SimpleNamespace(handle_single_url=AsyncMock())
        service, formatter = _make_service(url_handler=url_handler)
        message = SimpleNamespace()

        monkeypatch.setattr(
            service._store,
            "lookup_retry_url",
            AsyncMock(return_value="https://example.com/retry"),
        )

        handled = await service.handle_retry(
            message,
            uid=1,
            parts=["retry", "orig-cid"],
            correlation_id="cid-retry",
        )

        assert handled is True
        url_handler.handle_single_url.assert_awaited_once_with(
            message=message,
            url="https://example.com/retry",
            correlation_id="cid-retry",
        )
        assert formatter.safe_reply.await_args_list[0].args[1] == t("cb_retrying", "en")


class TestSummaryPayloadCache:
    @pytest.mark.asyncio
    async def test_returns_cached_payload_within_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, _ = _make_service()
        calls: list[str] = []

        async def _fake_load(summary_id: str) -> dict[str, Any] | None:
            calls.append(summary_id)
            return {"id": summary_id}

        times = iter([100.0, 100.5])
        monkeypatch.setattr(service._store, "_load_summary_payload", _fake_load)
        monkeypatch.setattr(callback_actions_module.time, "time", lambda: next(times))

        first = await service.load_summary_payload("42", correlation_id="cid")
        second = await service.load_summary_payload("42", correlation_id="cid")

        assert first == {"id": "42"}
        assert second == {"id": "42"}
        assert calls == ["42"]

    @pytest.mark.asyncio
    async def test_refreshes_cache_after_ttl_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, _ = _make_service()
        calls: list[str] = []

        async def _fake_load(summary_id: str) -> dict[str, Any] | None:
            calls.append(summary_id)
            return {"id": f"{summary_id}:{len(calls)}"}

        times = iter([100.0, 131.0])
        monkeypatch.setattr(service._store, "_load_summary_payload", _fake_load)
        monkeypatch.setattr(callback_actions_module.time, "time", lambda: next(times))

        first = await service.load_summary_payload("42", correlation_id="cid")
        second = await service.load_summary_payload("42", correlation_id="cid")

        assert first == {"id": "42:1"}
        assert second == {"id": "42:2"}
        assert calls == ["42", "42"]
