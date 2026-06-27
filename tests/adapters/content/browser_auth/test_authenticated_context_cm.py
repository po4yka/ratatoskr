"""Unit tests for the authenticated_context() async context manager.

Kept separate from test_authenticated_context.py (which covers
PlaywrightAuthedFetcher) so concerns stay isolated.

Patching strategy: the function does
    from playwright.async_api import Error as PWError, async_playwright
inside its body, so we patch playwright.async_api.async_playwright directly.
playwright.async_api.Error is the real exception class (playwright is installed).
"""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from app.adapters.content.browser_auth.authenticated_context import authenticated_context

_PW_MODULE = "playwright.async_api"
_DOMAIN = "example.com"
_ENDPOINT = "http://cloakserve:9222"
_SS_DATA: dict = {"cookies": [{"name": "sid", "value": "abc"}], "origins": []}


def _make_mocks(storage_state_return: dict | None = None) -> tuple:
    """Return a fully-wired Playwright mock chain.

    Returns (mock_async_playwright, mock_p, mock_browser, mock_context, mock_page).
    """
    ss_return = storage_state_return if storage_state_return is not None else _SS_DATA

    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_browser = AsyncMock()
    mock_p = MagicMock()

    mock_p.chromium.connect_over_cdp = AsyncMock(return_value=mock_browser)
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_page.route = AsyncMock()
    mock_context.storage_state = AsyncMock(return_value=ss_return)
    mock_context.close = AsyncMock()
    mock_browser.close = AsyncMock()

    # async_playwright() is used as: async with async_playwright() as p
    mock_pw_cm = AsyncMock()
    mock_pw_cm.__aenter__ = AsyncMock(return_value=mock_p)
    mock_pw_cm.__aexit__ = AsyncMock(return_value=False)
    mock_async_playwright = MagicMock(return_value=mock_pw_cm)

    return mock_async_playwright, mock_p, mock_browser, mock_context, mock_page


async def test_call_ordering_pre_and_post_yield() -> None:
    """Verifies the full call sequence relative to the yield point.

    Pre-yield: connect_over_cdp -> new_context -> new_page -> page.route
    Post-yield: context.storage_state -> context.close -> browser.close
    """
    mock_ap, mock_p, mock_browser, mock_context, mock_page = _make_mocks()
    order: list[str] = []

    async def _ss() -> dict:
        order.append("storage_state")
        return {"cookies": [], "origins": []}

    async def _ctx_close() -> None:
        order.append("context_close")

    async def _browser_close() -> None:
        order.append("browser_close")

    mock_context.storage_state = AsyncMock(side_effect=_ss)
    mock_context.close = AsyncMock(side_effect=_ctx_close)
    mock_browser.close = AsyncMock(side_effect=_browser_close)

    with patch(f"{_PW_MODULE}.async_playwright", mock_ap):
        async with authenticated_context(
            _DOMAIN,
            {"cookies": [], "origins": []},
            endpoint_url=_ENDPOINT,
        ) as (page, ctx):
            # All setup calls must have happened before the yield.
            mock_p.chromium.connect_over_cdp.assert_called_once()
            mock_browser.new_context.assert_called_once()
            mock_context.new_page.assert_called_once()
            mock_page.route.assert_called_once_with("**/*", ANY)
            # Teardown must NOT have run yet.
            mock_context.close.assert_not_called()
            mock_browser.close.assert_not_called()
            # Yielded objects are the right mocks.
            assert page is mock_page
            assert ctx is mock_context

    # storage_state exported first, context closed second, browser closed last.
    assert order == ["storage_state", "context_close", "browser_close"]


async def test_refreshed_out_receives_storage_state_dict() -> None:
    """refreshed_out is populated with the dict from context.storage_state() before close."""
    mock_ap, _, _, mock_context, _ = _make_mocks(storage_state_return=_SS_DATA)

    # Verify that storage_state is appended BEFORE context.close() by tracking order.
    close_calls_at_export: list[int] = []

    original_ss = mock_context.storage_state

    async def _ss_tracking() -> dict:
        close_calls_at_export.append(mock_context.close.call_count)
        return await original_ss()

    mock_context.storage_state = AsyncMock(side_effect=_ss_tracking)

    with patch(f"{_PW_MODULE}.async_playwright", mock_ap):
        refreshed: list[dict] = []
        async with authenticated_context(
            _DOMAIN,
            _SS_DATA,
            endpoint_url=_ENDPOINT,
            refreshed_out=refreshed,
        ):
            pass

    assert len(refreshed) == 1
    assert refreshed[0] == _SS_DATA
    # close had not yet been called when storage_state was exported.
    assert close_calls_at_export == [0]


async def test_browser_close_called_once_stop_never_called() -> None:
    """browser.close() is called exactly once; browser.stop() is never called."""
    mock_ap, _, mock_browser, _, _ = _make_mocks()

    with patch(f"{_PW_MODULE}.async_playwright", mock_ap):
        async with authenticated_context(_DOMAIN, None, endpoint_url=_ENDPOINT):
            pass

    mock_browser.close.assert_called_once()
    mock_browser.stop.assert_not_called()


async def test_storage_state_none_omits_kwarg() -> None:
    """When storage_state=None, new_context() must NOT receive a storage_state kwarg."""
    mock_ap, _, mock_browser, _, _ = _make_mocks()

    with patch(f"{_PW_MODULE}.async_playwright", mock_ap):
        async with authenticated_context(_DOMAIN, None, endpoint_url=_ENDPOINT):
            pass

    _, kwargs = mock_browser.new_context.call_args
    assert "storage_state" not in kwargs


async def test_storage_state_dict_forwarded_to_new_context() -> None:
    """When storage_state is a dict, it is forwarded as storage_state kwarg to new_context()."""
    ss = {"cookies": [{"name": "x", "value": "y"}], "origins": []}
    mock_ap, _, mock_browser, _, _ = _make_mocks()

    with patch(f"{_PW_MODULE}.async_playwright", mock_ap):
        async with authenticated_context(_DOMAIN, ss, endpoint_url=_ENDPOINT):
            pass

    _, kwargs = mock_browser.new_context.call_args
    assert kwargs.get("storage_state") is ss


async def test_non_pwerror_still_closes_context_and_browser() -> None:
    """If context.storage_state() raises a non-PWError, context.close() and browser.close() still run."""
    mock_ap, _, mock_browser, mock_context, _ = _make_mocks()
    mock_context.storage_state = AsyncMock(side_effect=RuntimeError("unexpected boom"))

    with patch(f"{_PW_MODULE}.async_playwright", mock_ap):
        with pytest.raises(RuntimeError, match="unexpected boom"):
            async with authenticated_context(_DOMAIN, None, endpoint_url=_ENDPOINT):
                pass

    mock_context.close.assert_called_once()
    mock_browser.close.assert_called_once()


async def test_async_playwright_used_as_async_context_manager() -> None:
    """async_playwright() must be called once and its result used as an async CM."""
    mock_ap, _, _, _, _ = _make_mocks()

    with patch(f"{_PW_MODULE}.async_playwright", mock_ap):
        async with authenticated_context(_DOMAIN, None, endpoint_url=_ENDPOINT):
            pass

    mock_ap.assert_called_once()
    pw_cm = mock_ap.return_value
    pw_cm.__aenter__.assert_called_once()
    pw_cm.__aexit__.assert_called_once()


async def test_refreshed_out_none_no_error() -> None:
    """When refreshed_out is omitted (None), no AttributeError is raised even with valid storage state."""
    mock_ap, _, _, _, _ = _make_mocks()

    with patch(f"{_PW_MODULE}.async_playwright", mock_ap):
        async with authenticated_context(_DOMAIN, None, endpoint_url=_ENDPOINT, refreshed_out=None):
            pass
