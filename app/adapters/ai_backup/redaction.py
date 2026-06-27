"""URL redaction helpers for AI-backup error messages.

Internal API paths, organization IDs, and cursor tokens embedded in error
messages must not leak into REST responses, Telegram output, or dashboards.
``redact_urls`` strips every http(s) URL down to its ``scheme://host`` before
the message is written to ``ai_account_backups.last_error``.
"""

from __future__ import annotations

import re
import urllib.parse

# Matches http:// or https:// followed by non-whitespace characters.
_URL_RE = re.compile(r"https?://\S+")

__all__ = ["redact_urls"]


def redact_urls(text: str | None) -> str | None:
    """Return *text* with every http(s) URL replaced by ``scheme://host``.

    Path, query string, and fragment are removed.  ``None`` is passed through
    unchanged so callers can use the result directly in nullable DB columns.

    Examples::

        >>> redact_urls("HTTP 401 on https://claude.ai/api/orgs/abc?tree=True")
        'HTTP 401 on https://claude.ai'
        >>> redact_urls(None) is None
        True
    """
    if text is None:
        return None

    def _replace(match: re.Match[str]) -> str:
        parsed = urllib.parse.urlparse(match.group(0))
        return f"{parsed.scheme}://{parsed.netloc}"

    return _URL_RE.sub(_replace, text)
