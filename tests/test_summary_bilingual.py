"""Tests for full bilingual (English + Russian) summary delivery."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from app.adapters.content.summary_translation import translate_summary_to_ru_struct
from app.adapters.content.url_post_summary_task_service import URLPostSummaryTaskService
from app.adapters.external.formatting.summary.presenter_context import SummaryPresenterContext
from app.adapters.external.formatting.summary.structured_summary_flow import StructuredSummaryFlow
from app.adapters.external.formatting.summary.summary_blocks import SummaryBlocksPresenter


def _cfg(temperature: float = 0.2) -> Any:
    return SimpleNamespace(openrouter=SimpleNamespace(temperature=temperature))


class _FakeParsed:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return dict(self._payload)


class _FakeStructResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.parsed = _FakeParsed(payload)


class TestTranslateSummaryStruct(unittest.IsolatedAsyncioTestCase):
    async def test_returns_translated_dict_and_carries_url(self) -> None:
        client = SimpleNamespace(
            chat_structured=AsyncMock(
                return_value=_FakeStructResult({"summary_250": "Привет", "tldr_ru": "Привет"})
            )
        )
        out = await translate_summary_to_ru_struct(
            llm_client=cast("Any", client),
            summary={"summary_250": "Hello", "canonical_url": "https://x.test/a"},
            cfg=_cfg(),
            correlation_id="cid",
        )
        assert out is not None
        self.assertEqual(out["summary_250"], "Привет")
        # canonical_url is preserved even though the translator omitted it
        self.assertEqual(out["canonical_url"], "https://x.test/a")
        client.chat_structured.assert_awaited_once()

    async def test_none_client_returns_none(self) -> None:
        out = await translate_summary_to_ru_struct(
            llm_client=cast("Any", None), summary={"summary_250": "x"}, cfg=_cfg()
        )
        self.assertIsNone(out)

    async def test_empty_summary_returns_none(self) -> None:
        client = SimpleNamespace(chat_structured=AsyncMock())
        out = await translate_summary_to_ru_struct(
            llm_client=cast("Any", client), summary={}, cfg=_cfg()
        )
        self.assertIsNone(out)
        client.chat_structured.assert_not_awaited()

    async def test_llm_failure_returns_none(self) -> None:
        client = SimpleNamespace(chat_structured=AsyncMock(side_effect=RuntimeError("boom")))
        out = await translate_summary_to_ru_struct(
            llm_client=cast("Any", client), summary={"summary_250": "x"}, cfg=_cfg()
        )
        self.assertIsNone(out)


def _flow_with_lang(lang: str) -> tuple[StructuredSummaryFlow, Any, Any]:
    response_sender = SimpleNamespace(safe_reply=AsyncMock())
    text_processor = SimpleNamespace(
        send_long_text=AsyncMock(),
        sanitize_summary_text=lambda s: s,
    )
    ctx = SummaryPresenterContext(
        response_sender=cast("Any", response_sender),
        text_processor=cast("Any", text_processor),
        data_formatter=cast("Any", MagicMock()),
        verbosity_resolver=None,
        progress_tracker=None,
        topic_manager=None,
        lang=lang,
    )
    flow = StructuredSummaryFlow(ctx, blocks=SummaryBlocksPresenter(ctx))
    return flow, response_sender, text_processor


class TestSecondaryLanguageRendering(unittest.IsolatedAsyncioTestCase):
    async def test_renders_russian_labels_via_lang_override(self) -> None:
        flow, response_sender, text_processor = _flow_with_lang("en")
        shaped = {"summary_250": "Hello world", "topic_tags": ["ai", "ml"]}

        sent = await flow.send_secondary_language_summary(
            cast("Any", SimpleNamespace()),
            shaped,
            lang="ru",
            header="РУ",
            correlation_id="cid",
        )

        self.assertTrue(sent)
        response_sender.safe_reply.assert_awaited_once()
        text_processor.send_long_text.assert_awaited()
        rendered = " ".join(
            str(call.args[1]) for call in text_processor.send_long_text.await_args_list
        )
        self.assertIn("Теги", rendered)  # Russian label, proves the override
        self.assertNotIn("Tags", rendered)
        # The original flow context is not mutated
        self.assertEqual(flow._context.lang, "en")

    def test_with_lang_same_returns_self(self) -> None:
        flow, _, _ = _flow_with_lang("ru")
        self.assertIs(flow._with_lang("ru"), flow)
        self.assertIsNot(flow._with_lang("en"), flow)


def _make_post_service(
    *, bilingual: bool, struct_result: dict[str, Any] | None, cache_helper: Any = None
) -> Any:
    if struct_result is None:
        chat_structured = AsyncMock(side_effect=RuntimeError("fail"))
    else:
        chat_structured = AsyncMock(return_value=_FakeStructResult(struct_result))
    response_formatter = SimpleNamespace(
        is_reader_mode=AsyncMock(return_value=True),
        safe_reply=AsyncMock(),
        send_secondary_language_summary=AsyncMock(return_value=True),
    )
    summary_delivery = SimpleNamespace(schedule_task=MagicMock(return_value=None))
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(summary_bilingual_enabled=bilingual),
        openrouter=SimpleNamespace(temperature=0.2),
    )
    summary_repo = SimpleNamespace(async_update_ru_payload=AsyncMock())
    svc = URLPostSummaryTaskService(
        response_formatter=cast("Any", response_formatter),
        summary_repo=cast("Any", summary_repo),
        article_generator=cast("Any", SimpleNamespace()),
        insights_generator=cast("Any", SimpleNamespace()),
        summary_delivery=cast("Any", summary_delivery),
        cfg=cfg,
        llm_client=cast("Any", SimpleNamespace(chat_structured=chat_structured)),
        cache_helper=cache_helper,
    )
    # Stub the fire-and-forget handlers so the gate test never spawns real work.
    svc._handle_additional_insights = MagicMock(return_value=None)  # type: ignore[method-assign]
    svc._handle_custom_article = MagicMock(return_value=None)  # type: ignore[method-assign]
    svc._run_related_reads = MagicMock(return_value=None)  # type: ignore[method-assign]
    svc._maybe_send_russian_translation = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return svc, response_formatter, summary_delivery


def _scheduled_labels(summary_delivery: Any) -> list[str]:
    return [call.args[3] for call in summary_delivery.schedule_task.call_args_list]


class TestPostSummaryBilingualGate(unittest.IsolatedAsyncioTestCase):
    async def test_bilingual_on_sends_full_russian_block(self) -> None:
        svc, rf, delivery = _make_post_service(
            bilingual=True, struct_result={"summary_250": "Привет"}
        )
        await svc.schedule_tasks(
            cast("Any", SimpleNamespace()),
            "content",
            "en",
            42,
            "cid",
            {"summary_250": "Hi", "key_ideas": []},
            needs_ru_translation=True,
            silent=False,
            url_hash="u:1",
        )
        rf.send_secondary_language_summary.assert_awaited_once()
        cast("Any", svc._summary_repo).async_update_ru_payload.assert_awaited_once()
        self.assertNotIn("ru_translation", _scheduled_labels(delivery))

    async def test_struct_failure_falls_back_to_prose(self) -> None:
        svc, rf, delivery = _make_post_service(bilingual=True, struct_result=None)
        await svc.schedule_tasks(
            cast("Any", SimpleNamespace()),
            "content",
            "en",
            42,
            "cid",
            {"summary_250": "Hi"},  # no tldr_ru -> prose fallback allowed
            needs_ru_translation=True,
            silent=False,
            url_hash="u:1",
        )
        rf.send_secondary_language_summary.assert_not_awaited()
        self.assertIn("ru_translation", _scheduled_labels(delivery))

    async def test_bilingual_off_uses_prose_path(self) -> None:
        svc, rf, delivery = _make_post_service(
            bilingual=False, struct_result={"summary_250": "Привет"}
        )
        await svc.schedule_tasks(
            cast("Any", SimpleNamespace()),
            "content",
            "en",
            42,
            "cid",
            {"summary_250": "Hi"},
            needs_ru_translation=True,
            silent=False,
            url_hash="u:1",
        )
        rf.send_secondary_language_summary.assert_not_awaited()
        self.assertIn("ru_translation", _scheduled_labels(delivery))


class TestSummaryForDelivery(unittest.TestCase):
    def test_strips_tldr_ru_when_suppressing(self) -> None:
        from app.adapters.content.graph_url_processor import _summary_for_delivery

        original = {"summary_250": "Hi", "tldr_ru": "Привет"}
        out = _summary_for_delivery(original, suppress_tldr_ru=True)
        self.assertNotIn("tldr_ru", out)
        self.assertEqual(out["summary_250"], "Hi")
        # original is not mutated
        self.assertIn("tldr_ru", original)

    def test_keeps_tldr_ru_when_not_suppressing(self) -> None:
        from app.adapters.content.graph_url_processor import _summary_for_delivery

        original = {"summary_250": "Hi", "tldr_ru": "Привет"}
        out = _summary_for_delivery(original, suppress_tldr_ru=False)
        self.assertIs(out, original)

    def test_noop_when_no_tldr_ru(self) -> None:
        from app.adapters.content.graph_url_processor import _summary_for_delivery

        original = {"summary_250": "Hi"}
        out = _summary_for_delivery(original, suppress_tldr_ru=True)
        self.assertIs(out, original)


class TestBilingualCache(unittest.IsolatedAsyncioTestCase):
    async def _run(self, *, cache_helper: Any) -> Any:
        svc, rf, _ = _make_post_service(
            bilingual=True, struct_result={"summary_250": "Привет"}, cache_helper=cache_helper
        )
        await svc.schedule_tasks(
            cast("Any", SimpleNamespace()),
            "content",
            "en",
            42,
            "cid",
            {"summary_250": "Hi"},
            needs_ru_translation=True,
            silent=False,
            url_hash="u:1",
        )
        return svc, rf

    async def test_cache_hit_skips_translation(self) -> None:
        cache_helper = SimpleNamespace(
            get_cached_ru_struct=AsyncMock(return_value={"summary_250": "Из кеша"}),
            write_ru_struct_cache=AsyncMock(),
        )
        svc, rf = await self._run(cache_helper=cache_helper)
        cast("Any", svc._llm_client).chat_structured.assert_not_awaited()
        cache_helper.write_ru_struct_cache.assert_not_awaited()
        rf.send_secondary_language_summary.assert_awaited_once()

    async def test_cache_miss_translates_and_writes(self) -> None:
        cache_helper = SimpleNamespace(
            get_cached_ru_struct=AsyncMock(return_value=None),
            write_ru_struct_cache=AsyncMock(),
        )
        svc, rf = await self._run(cache_helper=cache_helper)
        cast("Any", svc._llm_client).chat_structured.assert_awaited_once()
        cache_helper.write_ru_struct_cache.assert_awaited_once()
        rf.send_secondary_language_summary.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
