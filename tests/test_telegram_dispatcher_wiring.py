from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.adapters.telegram.command_dispatch import (
    TelegramCommandContribution,
    TextCommandRoute,
    UidCommandRoute,
    merge_command_contributions,
)
from app.di.telegram import _build_command_dispatcher_deps
from app.di.types import TelegramRepositories
from tests.conftest import make_test_app_config


def test_fake_command_contribution_merges_into_routes() -> None:
    async def _uid_handler(*_args: object) -> None:
        return None

    async def _text_handler(*_args: object) -> None:
        return None

    routes = merge_command_contributions(
        (
            TelegramCommandContribution(
                name="fake-early",
                pre_alias_uid=(UidCommandRoute("/fake", _uid_handler),),
                summarize_prefix="/summarize",
            ),
            TelegramCommandContribution(
                name="fake-late",
                post_summarize_text=(TextCommandRoute("/fake_late", _text_handler),),
            ),
        )
    )

    assert [route.prefix for route in routes.pre_alias_uid] == ["/fake"]
    assert routes.summarize_prefix == "/summarize"
    assert [route.prefix for route in routes.post_summarize_text] == ["/fake_late"]


def test_command_dispatcher_routes_preserve_expected_precedence_order() -> None:
    cfg = make_test_app_config()
    repositories = TelegramRepositories(
        user_repository=MagicMock(),
        summary_repository=MagicMock(),
        request_repository=MagicMock(),
        crawl_result_repository=MagicMock(),
        llm_repository=MagicMock(),
        tag_repository=MagicMock(),
        audit_log_repository=MagicMock(),
        batch_session_repository=MagicMock(),
    )
    application_services = SimpleNamespace(
        unread_summaries=MagicMock(),
        mark_summary_as_read=MagicMock(),
        event_bus=MagicMock(),
        search_topics=MagicMock(),
        social_auth=MagicMock(),
    )

    deps = _build_command_dispatcher_deps(
        cfg=cfg,
        db=MagicMock(),
        response_formatter=MagicMock(),
        audit_func=MagicMock(),
        url_processor=MagicMock(),
        url_handler=MagicMock(),
        aggregation_handler=MagicMock(),
        topic_searcher=MagicMock(),
        local_searcher=MagicMock(),
        task_manager=MagicMock(),
        hybrid_search=MagicMock(),
        verbosity_resolver=MagicMock(),
        application_services=application_services,
        repositories=repositories,
        tts_service_factory=lambda: MagicMock(),
    )

    assert [route.prefix for route in deps.routes.pre_alias_uid] == [
        "/start",
        "/help",
        "/dbinfo",
        "/dbverify",
        "/models",
        "/setmodel",
        "/clearcache",
    ]
    assert [route.prefix for route in deps.routes.pre_alias_text] == ["/admin"]
    assert [route.aliases for route in deps.routes.local_search_aliases] == [
        ("/finddb", "/findlocal")
    ]
    assert [route.aliases for route in deps.routes.online_search_aliases] == [
        ("/findweb", "/findonline", "/find")
    ]
    assert [route.prefix for route in deps.routes.pre_summarize_text] == [
        "/aggregate",
        "/summarize_all",
        "/retry",
    ]
    assert deps.routes.summarize_prefix == "/summarize"
    assert [route.prefix for route in deps.routes.post_summarize_uid] == ["/cancel"]
    assert [route.prefix for route in deps.routes.post_summarize_text] == [
        "/untag",
        "/tags",
        "/tag",
        "/unread",
        "/read",
        "/search",
        "/listen",
        "/cdigest",
        "/digest",
        "/channels",
        "/subscribe",
        "/unsubscribe",
        "/init_session",
        "/social",
        "/connect_x",
        "/connect_threads",
        "/connect_instagram",
        "/disconnect_social",
        "/settings",
        "/rules",
        "/export",
        "/backups",
        "/backup",
        "/substack",
        "/rss",
    ]
    assert [route.prefix for route in deps.routes.tail_uid] == ["/debug"]
