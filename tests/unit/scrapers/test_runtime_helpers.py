"""Unit tests for scraper helper modules.

Covers ``runtime_tuning``, ``attempt_log``, ``disabled_provider``, and the
``SCRAPER_PROVIDER_ORDER`` envvar reading path on ``ScraperConfig``. All
offline.
"""

from __future__ import annotations

import pytest

from app.adapters.content.scraper.attempt_log import (
    ScraperAttemptEntry,
    ScraperAttemptRecorder,
    serialize_attempt_log,
)
from app.adapters.content.scraper.disabled_provider import DisabledScraperProvider
from app.adapters.content.scraper.runtime_tuning import (
    BROWSER_PROVIDERS,
    is_js_heavy_url,
    normalize_hosts,
    normalize_profile,
    profile_retry_budget,
    profile_timeout_multiplier,
    tuned_firecrawl_wait_for_ms,
    tuned_provider_timeout,
)
from app.core.call_status import CallStatus

pytestmark = pytest.mark.no_network


# ---------------------------------------------------------------------------
# runtime_tuning — profile helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("fast", "fast"),
        ("BALANCED", "balanced"),
        ("  robust  ", "robust"),
        ("nonsense", "balanced"),
        ("", "balanced"),
    ],
)
def test_normalize_profile_canonicalizes_known_values(raw: str, expected: str) -> None:
    assert normalize_profile(raw) == expected


def test_profile_timeout_multiplier_returns_expected_values() -> None:
    assert profile_timeout_multiplier("fast") == 0.75
    assert profile_timeout_multiplier("balanced") == 1.0
    assert profile_timeout_multiplier("robust") == 1.35
    # Unknown profile falls back to balanced multiplier.
    assert profile_timeout_multiplier("???") == 1.0


def test_profile_retry_budget_caps_fast_profile_to_one_retry() -> None:
    assert profile_retry_budget(5, "fast") == 1
    assert profile_retry_budget(0, "fast") == 0


def test_profile_retry_budget_extends_robust_profile_within_cap() -> None:
    assert profile_retry_budget(2, "robust") == 3
    # Even very large bases cap at 5.
    assert profile_retry_budget(10, "robust") == 5


def test_profile_retry_budget_is_identity_for_balanced() -> None:
    assert profile_retry_budget(3, "balanced") == 3


def test_profile_retry_budget_clamps_negative_to_zero() -> None:
    assert profile_retry_budget(-5, "balanced") == 0


# ---------------------------------------------------------------------------
# runtime_tuning — host helpers
# ---------------------------------------------------------------------------


def test_normalize_hosts_lowercases_strips_and_dedupes() -> None:
    out = normalize_hosts(("X.com", "  x.com  ", "example.com", ""))
    assert out == ("example.com", "x.com")


def test_normalize_hosts_handles_empty_input() -> None:
    assert normalize_hosts(()) == ()


@pytest.mark.parametrize(
    ("url", "hosts", "expected"),
    [
        ("https://x.com/page", ("x.com",), True),
        ("https://X.com/page", ("x.com",), True),
        # Subdomain match via dotted suffix.
        ("https://mobile.x.com/page", ("x.com",), True),
        # Unrelated host returns False.
        ("https://example.com/page", ("x.com",), False),
        # Hostless URL returns False.
        ("not-a-url", ("x.com",), False),
    ],
)
def test_is_js_heavy_url_matches_host_and_subdomains(
    url: str, hosts: tuple[str, ...], expected: bool
) -> None:
    assert is_js_heavy_url(url, hosts) is expected


# ---------------------------------------------------------------------------
# runtime_tuning — tuned timeouts
# ---------------------------------------------------------------------------


def test_tuned_provider_timeout_applies_profile_multiplier() -> None:
    out = tuned_provider_timeout(
        base_timeout_sec=10.0,
        profile="robust",
        provider="defuddle",
        url="https://example.com/a",
        js_heavy_hosts=(),
    )
    assert out == pytest.approx(13.5)


def test_tuned_provider_timeout_speeds_up_scrapling_on_js_heavy_url() -> None:
    base = tuned_provider_timeout(
        base_timeout_sec=10.0,
        profile="balanced",
        provider="scrapling",
        url="https://x.com/",
        js_heavy_hosts=("x.com",),
    )
    # 10 * 1.0 * 0.8 == 8
    assert base == pytest.approx(8.0)


def test_tuned_provider_timeout_extends_browser_provider_on_js_heavy_url() -> None:
    base = tuned_provider_timeout(
        base_timeout_sec=10.0,
        profile="balanced",
        provider="playwright",
        url="https://x.com/",
        js_heavy_hosts=("x.com",),
    )
    # 10 * 1.0 * 1.25 == 12.5
    assert base == pytest.approx(12.5)


def test_tuned_provider_timeout_floors_at_one_second() -> None:
    out = tuned_provider_timeout(
        base_timeout_sec=0.1,
        profile="fast",
        provider="defuddle",
        url="https://example.com/a",
        js_heavy_hosts=(),
    )
    assert out >= 1.0


def test_tuned_firecrawl_wait_for_ms_extends_for_js_heavy_url() -> None:
    out = tuned_firecrawl_wait_for_ms(
        base_wait_for_ms=1000,
        url="https://x.com/",
        js_heavy_hosts=("x.com",),
    )
    assert out == 1300


def test_tuned_firecrawl_wait_for_ms_returns_zero_for_zero_base() -> None:
    assert (
        tuned_firecrawl_wait_for_ms(
            base_wait_for_ms=0, url="https://x.com/", js_heavy_hosts=("x.com",)
        )
        == 0
    )


def test_tuned_firecrawl_wait_for_ms_caps_at_ten_seconds() -> None:
    # base*1.3 above 10s should clamp to 10000.
    out = tuned_firecrawl_wait_for_ms(
        base_wait_for_ms=20000,
        url="https://x.com/",
        js_heavy_hosts=("x.com",),
    )
    assert out == 10000


def test_browser_providers_constant_matches_expected_set() -> None:
    # cloakbrowser is a CDP-sidecar browser driver and benefits from the
    # JS-heavy reorder alongside playwright and crawlee; scrapegraph_ai does
    # not (it is an LLM fallback, not a browser driver).
    assert frozenset({"playwright", "crawlee", "cloakbrowser"}) == BROWSER_PROVIDERS


# ---------------------------------------------------------------------------
# attempt_log
# ---------------------------------------------------------------------------


def test_attempt_entry_accepts_known_statuses() -> None:
    e = ScraperAttemptEntry(provider="p", status="success", latency_ms=10, error_class=None)
    assert e.provider == "p" and e.status == "success"


def test_attempt_entry_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="unknown scraper status"):
        ScraperAttemptEntry(provider="p", status="weird", latency_ms=0, error_class=None)


def test_attempt_recorder_records_entries_in_order() -> None:
    r = ScraperAttemptRecorder()
    r.record(ScraperAttemptEntry("p1", "error", 50, "Timeout"))
    r.record(ScraperAttemptEntry("p2", "success", 80, None))
    assert [e.provider for e in r.entries] == ["p1", "p2"]


def test_attempt_recorder_winner_is_first_success() -> None:
    r = ScraperAttemptRecorder()
    r.record(ScraperAttemptEntry("p1", "error", 50, "X"))
    r.record(ScraperAttemptEntry("p2", "success", 80, None))
    r.record(ScraperAttemptEntry("p3", "skipped", 0, None))
    assert r.winner() == "p2"


def test_attempt_recorder_winner_is_none_when_all_failed() -> None:
    r = ScraperAttemptRecorder()
    r.record(ScraperAttemptEntry("p1", "error", 10, "X"))
    r.record(ScraperAttemptEntry("p2", "timeout", 20, "T"))
    assert r.winner() is None


def test_attempt_recorder_failed_providers_excludes_winner() -> None:
    r = ScraperAttemptRecorder()
    r.record(ScraperAttemptEntry("a", "error", 1, "X"))
    r.record(ScraperAttemptEntry("b", "success", 2, None))
    r.record(ScraperAttemptEntry("c", "skipped", 0, None))
    assert r.failed_providers() == ["a", "c"]


def test_serialize_attempt_log_emits_plain_dicts() -> None:
    entries = [
        ScraperAttemptEntry("p", "error", 100, "Timeout"),
        ScraperAttemptEntry("q", "success", 200, None),
    ]
    out = serialize_attempt_log(entries)
    assert out == [
        {"provider": "p", "status": "error", "latency_ms": 100, "error_class": "Timeout"},
        {"provider": "q", "status": "success", "latency_ms": 200, "error_class": None},
    ]


# ---------------------------------------------------------------------------
# DisabledScraperProvider
# ---------------------------------------------------------------------------


def test_disabled_provider_name_is_scraper_disabled() -> None:
    assert DisabledScraperProvider().provider_name == "scraper_disabled"


@pytest.mark.asyncio
async def test_disabled_provider_returns_error_result_with_configured_reason() -> None:
    p = DisabledScraperProvider(reason="disabled by config")
    result = await p.scrape_markdown("https://example.com/x", mobile=True, request_id=42)
    assert result.status == CallStatus.ERROR
    assert result.error_text == "disabled by config"
    assert result.source_url == "https://example.com/x"
    assert result.endpoint == "scraper_disabled"


@pytest.mark.asyncio
async def test_disabled_provider_aclose_is_noop() -> None:
    p = DisabledScraperProvider()
    # Must not raise.
    await p.aclose()
