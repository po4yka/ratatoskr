"""Host-allowlist guard for AI account backup internal-API calls.

Pure, synchronous, no I/O. This is a defence-in-depth layer on top of (not a
replacement for) the SSRF check in ``app.security.ssrf``: it restricts the
backup clients to the providers' own hosts so a tampered response cannot
redirect a fetch to an arbitrary public host.
"""

from __future__ import annotations

from urllib.parse import urlparse

from app.adapters.ai_backup.errors import AiBackupHostDeniedError


def assert_host_allowed(url: str, allowlist: list[str]) -> None:
    """Raise ``AiBackupHostDeniedError`` if ``url``'s host is not permitted.

    Matching rules (case-insensitive on the hostname):
    - Exact entry ``chatgpt.com`` matches only ``chatgpt.com``.
    - Wildcard entry ``*.oaiusercontent.com`` matches any subdomain
      (``files.oaiusercontent.com``) and the apex ``oaiusercontent.com``.

    The hostname is taken from ``urlparse(...).hostname`` so userinfo tricks like
    ``https://chatgpt.com@evil.com/`` resolve to ``evil.com`` and are rejected.
    The error message never includes the full URL or any credentials.
    """
    host = (urlparse(url).hostname or "").lower()
    for pattern in allowlist:
        p = pattern.lower()
        if p.startswith("*."):
            suffix = p[2:]
            if host == suffix or host.endswith("." + suffix):
                return
        elif host == p:
            return
    raise AiBackupHostDeniedError(
        f"Host {host!r} not in allowlist; add it to AI_BACKUP_HOST_ALLOWLIST"
    )


__all__ = ["assert_host_allowed"]
