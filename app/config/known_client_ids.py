"""Canonical registry of client_ids shipped by the official Ratatoskr clients.

This is the static source of truth for which client_ids the official builds
send. A deployment's ALLOWED_CLIENT_IDS must be a superset of this set (or
AUTH_ALLOW_ANY_CLIENT_ID=true must be set) for all official clients to work.

When a client ships a new default client_id:
  1. Add it here with a comment mapping it to the client that ships it.
  2. Add it to every deployment's ALLOWED_CLIENT_IDS env var (or set
     AUTH_ALLOW_ANY_CLIENT_ID=true for development/local deployments).
"""

from __future__ import annotations

KNOWN_CLIENT_IDS: frozenset[str] = frozenset(
    {
        "ratatoskr-android-v1.0",  # Android app (ratatoskr-client)
        "ratatoskr-ios-v1.0",  # iOS app (ratatoskr-client)
        "web-v1",  # Web SPA (ratatoskr-web)
        "browser-extension",  # Manifest V3 quick-save browser extension
    }
)
