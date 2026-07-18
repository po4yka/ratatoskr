"""Tests for RSS configuration."""

import pytest
from pydantic import ValidationError

from app.config.rss import RSSConfig


class TestRSSConfig:
    def test_defaults(self) -> None:
        cfg = RSSConfig()
        assert cfg.enabled is False
        assert cfg.poll_interval_minutes == 30
        assert cfg.auto_summarize is True
        assert cfg.min_content_length == 500
        assert cfg.max_items_per_poll == 20
        assert cfg.poll_concurrency == 8
        assert cfg.concurrency == 2

    def test_enabled_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = RSSConfig(enabled=True, poll_interval_minutes=15)
        assert cfg.enabled is True
        assert cfg.poll_interval_minutes == 15

    def test_poll_interval_bounds(self) -> None:
        with pytest.raises(ValidationError):
            RSSConfig(poll_interval_minutes=1)  # below min of 5
        with pytest.raises(ValidationError):
            RSSConfig(poll_interval_minutes=2000)  # above max of 1440

    def test_concurrency_bounds(self) -> None:
        with pytest.raises(ValidationError):
            RSSConfig(concurrency=0)
        cfg = RSSConfig(concurrency=5)
        assert cfg.concurrency == 5

    def test_poll_concurrency_is_separate_and_bounded(self) -> None:
        with pytest.raises(ValidationError):
            RSSConfig(poll_concurrency=0)
        with pytest.raises(ValidationError):
            RSSConfig(poll_concurrency=33)
        cfg = RSSConfig(poll_concurrency=6, concurrency=2)
        assert cfg.poll_concurrency == 6
        assert cfg.concurrency == 2

    def test_frozen(self) -> None:
        cfg = RSSConfig()
        with pytest.raises(ValidationError):
            cfg.enabled = True
