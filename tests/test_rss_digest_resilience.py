"""RSS and digest failure paths that quietly cost data or disabled a source.

A 301 to a canonical host counted as a fetch failure and auto-disabled the feed;
an unbounded body could be buffered whole; one feed's error aborted the whole
poll cycle; a long FloodWait was recorded as "channel unreachable" and after a
few digests disabled it for good; a send that failed midway lost the record of
the chunks already delivered; and a 200 with an unreadable body made a sent
email look failed, so the next digest sent it again.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.digest.channel_reader import _fetch_error_reason
from app.adapters.rss import feed_fetcher


class _FloodWaitError(Exception):
    """Shaped like telethon's FloodWaitError."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"A wait of {seconds} seconds is required")
        self.seconds = seconds


class TestFetchErrorClassification:
    def test_flood_wait_is_a_rate_limit(self) -> None:
        assert _fetch_error_reason(_FloodWaitError(1800)) == "rate_limited"

    def test_too_many_requests_text_is_a_rate_limit(self) -> None:
        assert _fetch_error_reason(RuntimeError("429 Too Many Requests")) == "rate_limited"

    def test_anything_else_is_a_fetch_failure(self) -> None:
        assert _fetch_error_reason(RuntimeError("channel not found")) == "fetch_failed"


class TestFeedBodyCap:
    def test_declared_oversize_is_refused_before_reading(self) -> None:
        resp = MagicMock()
        resp.headers = {"content-length": str(feed_fetcher._MAX_FEED_BYTES + 1)}
        resp.iter_bytes = MagicMock(return_value=iter([b"x"]))

        with pytest.raises(ValueError, match="cap"):
            feed_fetcher._read_capped(resp)

    def test_undeclared_oversize_is_refused_while_streaming(self) -> None:
        """Content-Length can be absent or wrong, so the count is authoritative."""
        chunk = b"x" * (1024 * 1024)
        resp = MagicMock()
        resp.headers = {}
        resp.iter_bytes = MagicMock(return_value=iter([chunk] * 20))

        with pytest.raises(ValueError, match="cap"):
            feed_fetcher._read_capped(resp)

    def test_a_normal_feed_is_returned_whole(self) -> None:
        resp = MagicMock()
        resp.headers = {}
        resp.iter_bytes = MagicMock(return_value=iter([b"<rss>", b"</rss>"]))
        assert feed_fetcher._read_capped(resp) == b"<rss></rss>"


class TestFeedRedirects:
    @staticmethod
    def _client(responses: list[Any]) -> Any:
        calls = iter(responses)

        def _stream(_method: str, url: str, **_kw: Any) -> Any:
            ctx = MagicMock()
            ctx.__enter__.return_value = next(calls)
            ctx.__exit__.return_value = None
            return ctx

        client = MagicMock()
        client.stream = _stream
        manager = MagicMock()
        manager.__enter__.return_value = client
        manager.__exit__.return_value = None
        return manager

    @staticmethod
    def _resp(status: int, *, location: str | None = None, body: bytes = b"") -> Any:
        resp = MagicMock()
        resp.status_code = status
        resp.headers = {"location": location} if location else {}
        resp.iter_bytes = MagicMock(return_value=iter([body] if body else []))
        resp.raise_for_status = MagicMock()
        return resp

    def test_a_permanent_redirect_is_followed(self) -> None:
        """Feedburner and Substack answer 301 to their canonical https host.

        Treating that as a failure ticked the error counter and auto-disabled
        the feed after roughly ten polls.
        """
        responses = [
            self._resp(301, location="https://example.com/feed.xml"),
            self._resp(200, body=b"<rss/>"),
        ]
        with (
            patch.object(
                feed_fetcher, "make_safe_sync_client", return_value=self._client(responses)
            ),
            patch.object(feed_fetcher, "_validate_feed_url"),
        ):
            fetched = feed_fetcher._fetch_feed_body(
                "http://example.com/feed", headers={}, timeout=5.0
            )
        assert fetched is not None
        assert fetched[0] == b"<rss/>"

    def test_a_redirect_loop_is_bounded(self) -> None:
        responses = [self._resp(301, location="https://example.com/again") for _ in range(10)]
        with (
            patch.object(
                feed_fetcher, "make_safe_sync_client", return_value=self._client(responses)
            ),
            patch.object(feed_fetcher, "_validate_feed_url"),
        ):
            with pytest.raises(ValueError, match="redirects"):
                feed_fetcher._fetch_feed_body("http://example.com/feed", headers={}, timeout=5.0)

    def test_every_redirect_target_is_revalidated(self) -> None:
        """Following redirects must not widen the SSRF surface."""
        responses = [
            self._resp(302, location="https://elsewhere.test/feed"),
            self._resp(200, body=b"<rss/>"),
        ]
        with (
            patch.object(
                feed_fetcher, "make_safe_sync_client", return_value=self._client(responses)
            ),
            patch.object(feed_fetcher, "_validate_feed_url") as validate,
        ):
            feed_fetcher._fetch_feed_body("http://example.com/feed", headers={}, timeout=5.0)
        validate.assert_called_once_with("https://elsewhere.test/feed")

    def test_304_short_circuits(self) -> None:
        with (
            patch.object(
                feed_fetcher, "make_safe_sync_client", return_value=self._client([self._resp(304)])
            ),
            patch.object(feed_fetcher, "_validate_feed_url"),
        ):
            assert (
                feed_fetcher._fetch_feed_body("http://example.com/feed", headers={}, timeout=5.0)
                is None
            )


@pytest.mark.asyncio
async def test_resend_treats_an_unreadable_200_as_sent() -> None:
    """The mail already went out; reporting failure is what duplicates it."""
    from app.adapters.email.protocol import EmailMessage
    from app.adapters.email.resend import ResendEmailProvider

    provider = object.__new__(ResendEmailProvider)
    provider._cfg = SimpleNamespace(
        resend_api_key="k",
        from_address="b@example.test",
        from_name="Ratatoskr",
        timeout_seconds=5,
        resend_api_url="https://api.resend.test/emails",
    )
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json = MagicMock(side_effect=ValueError("not json"))

    http = MagicMock()
    http.post = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=http)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.adapters.email.resend.httpx.AsyncClient", return_value=ctx):
        result = await provider.send(EmailMessage(to="a@example.test", subject="s", text="t"))

    assert result.status == "sent"
    assert result.provider_message_id is None


@pytest.mark.asyncio
async def test_one_broken_feed_does_not_abort_the_poll_cycle() -> None:
    """Without return_exceptions the aggregation never ran.

    new_item_ids was then never handed to source ingestion and delivery, and
    Taskiq retried the whole cycle up to three times.
    """
    from app.adapters.rss import feed_poller

    feeds = [{"id": 1, "url": "https://a.test/f"}, {"id": 2, "url": "https://b.test/f"}]

    async def _poll(_repo: Any, _signal_repo: Any, feed: dict[str, Any]) -> Any:
        if feed["id"] == 1:
            raise RuntimeError("feed 1 exploded")
        return feed_poller._FeedPollResult(polled=1, new_items=1, new_item_ids=[99])

    repo = MagicMock()
    repo.async_list_active_feeds = AsyncMock(return_value=feeds)

    with (
        patch.object(feed_poller, "RSSFeedRepositoryAdapter", return_value=repo),
        patch.object(feed_poller, "SignalSourceRepositoryAdapter", return_value=MagicMock()),
        patch.object(feed_poller, "_poll_feed", side_effect=_poll),
    ):
        stats = await feed_poller.poll_all_feeds(MagicMock())

    assert stats["errors"] == 1
    assert stats["polled"] == 1
    assert stats["new_item_ids"] == [99]


@pytest.mark.asyncio
async def test_a_send_failure_carries_the_delivered_count_out() -> None:
    """Two of three chunks landed; losing that count re-sends them next run."""
    from app.adapters.digest.digest_service import DigestService, _PartialDeliveryError

    svc = object.__new__(DigestService)
    svc._store = SimpleNamespace(  # type: ignore[attr-defined]
        async_get_user_preference=AsyncMock(
            return_value=SimpleNamespace(delivery_channel="telegram")
        )
    )
    calls = {"n": 0}

    async def _send(*_a: Any, **_kw: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("flood wait")

    svc._send = _send  # type: ignore[attr-defined]

    with pytest.raises(_PartialDeliveryError) as caught:
        await svc._deliver_digest_messages(
            user_id=1,
            message_chunks=[("a", []), ("b", []), ("c", [])],
            digest_type="daily",
            correlation_id="cid",
            post_count=3,
            channel_count=1,
        )

    assert caught.value.sent == 2
