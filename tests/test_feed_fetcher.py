"""Tests for RSS/Atom feed fetcher."""

from __future__ import annotations

import sys
import time
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from app.adapters.rss.feed_fetcher import FeedEntry, FeedResult, fetch_feed

# Ensure feedparser is available even if not installed.
# fetch_feed does a lazy `import feedparser` inside the function body,
# so we inject a mock module into sys.modules when it's missing.
if "feedparser" not in sys.modules:
    _mock_feedparser = ModuleType("feedparser")
    _mock_feedparser.parse = MagicMock()  # type: ignore[attr-defined]
    sys.modules["feedparser"] = _mock_feedparser


def _feedparser_entry(
    *,
    title: str | None = None,
    link: str | None = None,
    entry_id: str | None = None,
    summary: str | None = None,
    content: list | None = None,
    author: str | None = None,
    published_parsed: time.struct_time | None = None,
) -> dict:
    """Build a feedparser-style entry dict."""
    entry: dict = {}
    if title is not None:
        entry["title"] = title
    if link is not None:
        entry["link"] = link
    if entry_id is not None:
        entry["id"] = entry_id
    if summary is not None:
        entry["summary"] = summary
    if content is not None:
        entry["content"] = content
    if author is not None:
        entry["author"] = author

    class _Entry(dict):
        """Dict subclass mimicking feedparser's FeedParserDict.

        Supports both dict access (entry.get("key")) and attribute access
        (entry.key) as feedparser entries do.
        """

        def __init__(self, data: dict, pub: time.struct_time | None) -> None:
            super().__init__(data)
            self.published_parsed = pub

        def __getattr__(self, name: str) -> object:
            try:
                return self[name]
            except KeyError:
                raise AttributeError(name) from None

    return _Entry(entry, published_parsed)


def _feedparser_result(
    *,
    title: str = "",
    link: str = "",
    subtitle: str = "",
    entries: list | None = None,
) -> MagicMock:
    """Build a feedparser.parse() return value."""
    result = MagicMock()
    result.feed = {"title": title, "link": link, "subtitle": subtitle}
    result.entries = entries or []
    return result


class TestFeedEntryDataclass:
    def test_defaults(self) -> None:
        entry = FeedEntry(guid="123")
        assert entry.guid == "123"
        assert entry.title is None
        assert entry.url is None
        assert entry.content is None
        assert entry.author is None
        assert entry.published_at is None

    def test_all_fields(self) -> None:
        from datetime import UTC, datetime

        now = datetime.now(tz=UTC)
        entry = FeedEntry(
            guid="g1",
            title="Title",
            url="https://example.com",
            content="body",
            author="Alice",
            published_at=now,
        )
        assert entry.guid == "g1"
        assert entry.title == "Title"
        assert entry.url == "https://example.com"
        assert entry.content == "body"
        assert entry.author == "Alice"
        assert entry.published_at == now


class TestFeedResultDataclass:
    def test_defaults(self) -> None:
        result = FeedResult()
        assert result.title is None
        assert result.description is None
        assert result.site_url is None
        assert result.entries == []
        assert result.etag is None
        assert result.last_modified is None
        assert result.not_modified is False

    def test_with_entries(self) -> None:
        entries = [FeedEntry(guid="1", title="Test")]
        result = FeedResult(title="My Feed", entries=entries)
        assert len(result.entries) == 1
        assert result.title == "My Feed"

    def test_not_modified_flag(self) -> None:
        result = FeedResult(not_modified=True)
        assert result.not_modified is True
        assert result.entries == []


def _patch_ssrf():
    """Bypass SSRF validation in tests -- example.com is not resolvable in CI."""
    return patch(
        "app.adapters.rss.feed_fetcher.is_url_safe",
        return_value=(True, None),
    )


def _patch_feed_client(response: MagicMock):
    """Patch the safe HTTP client factory.

    The fetcher streams so an oversized feed is refused without being buffered,
    so the fake exposes ``client.stream`` as a context manager and gives the
    response an ``iter_bytes``. ``content`` set by a test is chunked through it.
    """
    if not isinstance(response.headers, dict):
        response.headers = {}
    raw = response.content if isinstance(response.content, bytes) else b""
    response.iter_bytes = MagicMock(return_value=iter([raw] if raw else []))

    stream_ctx = MagicMock()
    stream_ctx.__enter__.return_value = response
    stream_ctx.__exit__.return_value = None

    client = MagicMock()
    client.stream.return_value = stream_ctx
    manager = MagicMock()
    manager.__enter__.return_value = client
    manager.__exit__.return_value = None
    return patch("app.adapters.rss.feed_fetcher.make_safe_sync_client", return_value=manager)


class TestFetchFeed:
    def test_304_not_modified(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 304
        with (
            _patch_ssrf(),
            _patch_feed_client(mock_resp),
        ):
            result = fetch_feed("https://example.com/feed.xml", etag='"abc"')
            assert result.not_modified is True
            assert result.entries == []

    def test_successful_rss_fetch(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"<rss/>"
        mock_resp.headers = {
            "ETag": '"xyz"',
            "Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT",
        }
        mock_resp.raise_for_status = MagicMock()

        parsed = _feedparser_result(
            title="Test Feed",
            link="https://example.com",
            entries=[
                _feedparser_entry(
                    title="Article 1",
                    link="https://example.com/1",
                    entry_id="guid-1",
                    summary="Content here",
                ),
                _feedparser_entry(
                    title="Article 2",
                    link="https://example.com/2",
                    entry_id="guid-2",
                ),
            ],
        )

        with (
            _patch_ssrf(),
            _patch_feed_client(mock_resp),
            patch("feedparser.parse", return_value=parsed),
        ):
            result = fetch_feed("https://example.com/feed.xml")
            assert result.title == "Test Feed"
            assert len(result.entries) == 2
            assert result.entries[0].guid == "guid-1"
            assert result.entries[0].title == "Article 1"
            assert result.entries[0].url == "https://example.com/1"
            assert result.entries[0].content == "Content here"
            assert result.etag == '"xyz"'
            assert result.last_modified == "Thu, 01 Jan 2026 00:00:00 GMT"

    def test_atom_feed_parsing(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"<feed/>"
        mock_resp.headers = {}
        mock_resp.raise_for_status = MagicMock()

        parsed = _feedparser_result(
            title="Atom Feed",
            entries=[
                _feedparser_entry(
                    title="Atom Entry",
                    entry_id="urn:uuid:123",
                    link="https://example.com/atom/1",
                    summary="Atom content",
                ),
            ],
        )

        with (
            _patch_ssrf(),
            _patch_feed_client(mock_resp),
            patch("feedparser.parse", return_value=parsed),
        ):
            result = fetch_feed("https://example.com/atom.xml")
            assert result.title == "Atom Feed"
            assert len(result.entries) == 1
            assert result.entries[0].title == "Atom Entry"
            assert result.entries[0].guid == "urn:uuid:123"
            assert result.entries[0].content == "Atom content"

    def test_http_error_raises(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status.side_effect = Exception("Not Found")

        with (
            _patch_ssrf(),
            _patch_feed_client(mock_resp),
        ):
            with pytest.raises(Exception, match="Not Found"):
                fetch_feed("https://example.com/bad-feed")

    def test_conditional_headers_sent(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 304

        with (
            _patch_ssrf(),
            _patch_feed_client(mock_resp) as mock_client_factory,
        ):
            fetch_feed(
                "https://example.com/feed.xml",
                etag='"abc"',
                last_modified="Thu, 01 Jan 2026",
            )
            mock_get = mock_client_factory.return_value.__enter__.return_value.stream
            call_kwargs = mock_get.call_args
            headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
            assert headers.get("If-None-Match") == '"abc"'
            assert headers.get("If-Modified-Since") == "Thu, 01 Jan 2026"

    def test_no_conditional_headers_when_none(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 304

        with (
            _patch_ssrf(),
            _patch_feed_client(mock_resp) as mock_client_factory,
        ):
            fetch_feed("https://example.com/feed.xml")
            mock_get = mock_client_factory.return_value.__enter__.return_value.stream
            call_kwargs = mock_get.call_args
            headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
            assert "If-None-Match" not in headers
            assert "If-Modified-Since" not in headers
            assert "User-Agent" in headers

    def test_entry_without_guid_falls_back_to_link(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"<rss/>"
        mock_resp.headers = {}
        mock_resp.raise_for_status = MagicMock()

        parsed = _feedparser_result(
            title="No GUID Feed",
            entries=[
                _feedparser_entry(
                    title="No GUID Article",
                    link="https://example.com/no-guid",
                ),
            ],
        )

        with (
            _patch_ssrf(),
            _patch_feed_client(mock_resp),
            patch("feedparser.parse", return_value=parsed),
        ):
            result = fetch_feed("https://example.com/feed.xml")
            assert len(result.entries) == 1
            # guid falls back to link when no id element
            assert result.entries[0].guid == "https://example.com/no-guid"

    def test_entry_with_published_date(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"<rss/>"
        mock_resp.headers = {}
        mock_resp.raise_for_status = MagicMock()

        pub_time = time.strptime("2025-01-01 12:00:00", "%Y-%m-%d %H:%M:%S")
        parsed = _feedparser_result(
            title="Dated Feed",
            entries=[
                _feedparser_entry(
                    title="Dated Article",
                    entry_id="dated-1",
                    published_parsed=pub_time,
                ),
            ],
        )

        with (
            _patch_ssrf(),
            _patch_feed_client(mock_resp),
            patch("feedparser.parse", return_value=parsed),
        ):
            result = fetch_feed("https://example.com/feed.xml")
            assert len(result.entries) == 1
            assert result.entries[0].published_at is not None
            assert result.entries[0].published_at.year == 2025

    def test_entry_content_field_preferred_over_summary(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"<rss/>"
        mock_resp.headers = {}
        mock_resp.raise_for_status = MagicMock()

        parsed = _feedparser_result(
            title="Content Feed",
            entries=[
                _feedparser_entry(
                    title="Has Content",
                    entry_id="c1",
                    content=[{"value": "Full content body"}],
                    summary="Short summary",
                ),
            ],
        )

        with (
            _patch_ssrf(),
            _patch_feed_client(mock_resp),
            patch("feedparser.parse", return_value=parsed),
        ):
            result = fetch_feed("https://example.com/feed.xml")
            assert result.entries[0].content == "Full content body"

    def test_follow_redirects_and_timeout(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 304

        with (
            _patch_ssrf(),
            _patch_feed_client(mock_resp) as mock_client_factory,
        ):
            fetch_feed("https://example.com/feed.xml", timeout=15.0)
            mock_get = mock_client_factory.return_value.__enter__.return_value.stream
            call_kwargs = mock_get.call_args
            assert mock_client_factory.call_args.kwargs.get("follow_redirects") is False
            assert call_kwargs.kwargs.get("timeout") == 15.0
