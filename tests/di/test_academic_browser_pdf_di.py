"""DI wiring for the academic browser-PDF fetcher.

Locks in the decoupling: the academic recovery's CloakBrowser fetcher is built
from the academic flags + a CloakBrowser URL, INDEPENDENT of the scraper chain's
``cloakbrowser_enabled`` flag (so a deployment can keep CloakBrowser off as a
general scraper rung yet still use the sidecar for gated academic PDFs).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.config.academic import AcademicConfig
from app.di.platform_extractors import _build_academic_browser_pdf

pytestmark = pytest.mark.no_network


# Returns Any: the builder only duck-types ``context.cfg`` / ``context.audit_func``,
# so a SimpleNamespace stub is sufficient (and keeps mypy off the real dataclass).
def _ctx(
    *,
    browser_pdf_enabled: bool = False,
    agentic_enabled: bool = False,
    allowlist: tuple[str, ...] = (),
    cloakbrowser_url: str = "http://cloakbrowser:9222",
    cloakbrowser_enabled: bool = False,
) -> Any:
    scraper = SimpleNamespace(
        cloakbrowser_url=cloakbrowser_url,
        cloakbrowser_enabled=cloakbrowser_enabled,  # must be IGNORED by the builder
        cloakbrowser_timeout_sec=60,
        min_content_length=400,
        profile="balanced",
        js_heavy_hosts=(),
        cloakbrowser_humanize=True,
        cloakbrowser_proxy="",
    )
    academic = AcademicConfig(
        browser_pdf_recovery_enabled=browser_pdf_enabled,
        agentic_pdf_download_enabled=agentic_enabled,
        agentic_pdf_host_allowlist=allowlist,
    )
    cfg = SimpleNamespace(academic=academic, scraper=scraper)
    return SimpleNamespace(cfg=cfg, audit_func=lambda *a, **k: None)


def test_built_when_tier1_on_even_if_chain_cloakbrowser_disabled() -> None:
    prov = _build_academic_browser_pdf(_ctx(browser_pdf_enabled=True, cloakbrowser_enabled=False))
    assert prov is not None
    assert hasattr(prov, "fetch_pdf")
    assert hasattr(prov, "download_pdf_via_controls")


def test_built_when_only_tier2_on() -> None:
    prov = _build_academic_browser_pdf(
        _ctx(agentic_enabled=True, allowlist=("researchgate",), cloakbrowser_enabled=False)
    )
    assert prov is not None


def test_none_when_all_flags_off() -> None:
    assert _build_academic_browser_pdf(_ctx(cloakbrowser_enabled=True)) is None


def test_none_when_no_cloakbrowser_url() -> None:
    assert _build_academic_browser_pdf(_ctx(browser_pdf_enabled=True, cloakbrowser_url="")) is None
