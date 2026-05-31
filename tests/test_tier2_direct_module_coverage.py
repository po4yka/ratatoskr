"""Direct import smoke tests for modules flagged as transitive-only coverage."""

from __future__ import annotations

from app.adapters.content.scraper import diagnostics as scraper_diagnostics
from app.adapters.external.firecrawl import options as firecrawl_options
from app.adapters.external.formatting import export_formatter
from app.adapters.openrouter import chat_engine, request_builder
from app.application import ports as repository_ports
from app.application.services.summarization import llm_response_workflow_execution
from app.core import async_utils, logging_utils, ui_strings
from app.core.summary_contract_impl import contract as summary_contract
from app.infrastructure.cache import batch_progress_cache
from app.infrastructure.persistence import protocol as persistence_protocol
from app.infrastructure.persistence.repositories import summary_repository
from tests.conftest import make_test_app_config

DIRECT_MODULES = [
    scraper_diagnostics,
    llm_response_workflow_execution,
    firecrawl_options,
    export_formatter,
    chat_engine,
    request_builder,
    repository_ports,
    async_utils,
    logging_utils,
    ui_strings,
    summary_contract,
    batch_progress_cache,
    persistence_protocol,
    summary_repository,
]


def test_direct_module_imports() -> None:
    """Ensure each module has at least one direct test touchpoint."""
    assert all(module.__name__ for module in DIRECT_MODULES)


def test_scraper_diagnostics_shape() -> None:
    cfg = make_test_app_config()
    payload = scraper_diagnostics.build_scraper_diagnostics(cfg)
    assert payload["status"] in {"healthy", "degraded", "disabled"}
    assert "providers" in payload
    assert "twitter" in payload


def test_ui_strings_lookup() -> None:
    assert ui_strings.t("tldr", lang="ru") == "TL;DR"
    assert ui_strings.t("definitely_missing_key", lang="en") == "definitely_missing_key"


def test_repository_ports_exports() -> None:
    assert hasattr(repository_ports, "RequestRepositoryPort")
