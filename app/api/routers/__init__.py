"""
API route handlers.

Domain grouping:
  content/  — summaries, requests, streams, search, aggregation
  user/     — user account, highlights, tags, tts
  social/   — digest, rss, signals
  auth/     — authentication and authorization
"""

from . import (
    admin,
    auth,
    backups,
    collections,
    custom_digests,
    health,
    import_export,
    meta,
    notifications,
    proxy,
    quick_save,
    rules,
    sync,
    system,
    webhooks,
)
from .content import aggregation, requests, search, streams, summaries
from .social import digest, rss, signals
from .user import highlights, tags, tts, user

__all__ = [
    "admin",
    "aggregation",
    "auth",
    "backups",
    "collections",
    "custom_digests",
    "digest",
    "health",
    "highlights",
    "import_export",
    "meta",
    "notifications",
    "proxy",
    "quick_save",
    "requests",
    "rss",
    "rules",
    "search",
    "signals",
    "streams",
    "summaries",
    "sync",
    "system",
    "tags",
    "tts",
    "user",
    "webhooks",
]
