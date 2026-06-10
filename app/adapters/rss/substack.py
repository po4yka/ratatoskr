"""Substack URL resolution utilities.

The canonical implementations live in ``app/core/substack_utils``; this module
re-exports them for backward compatibility within the ``rss`` adapter.
"""

from __future__ import annotations

from app.core.substack_utils import is_substack_url, resolve_substack_feed_url

__all__ = ["is_substack_url", "resolve_substack_feed_url"]
