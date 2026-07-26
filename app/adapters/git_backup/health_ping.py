"""Best-effort Healthchecks.io dead-man-switch pings for the git-backup sync job.

Three entry points mirror gitout's ping semantics:

- ``ping_start``   — POST {url}/start   (called before the sync begins)
- ``ping_success`` — POST {url}          (called after the sync completes)
- ``ping_failure`` — POST {url}/fail     (called when the sync raises an exception)

All three are best-effort: every network/timeout error is logged at WARNING and
swallowed. A failed ping MUST NOT affect the backup outcome.
"""

from __future__ import annotations

from app.core.logging_utils import get_logger
from app.security.ssrf import make_safe_async_client

logger = get_logger(__name__)


async def _post(url: str, timeout: float, *, label: str, body: str | None = None) -> None:
    """POST to *url* and swallow all errors."""
    try:
        async with make_safe_async_client(timeout=timeout) as client:
            content = body.encode() if body else None
            await client.post(url, content=content)
    except Exception as exc:  # intentional broad catch; must never raise
        logger.warning(
            "git_backup_health_ping_failed",
            extra={"ping": label, "error_type": type(exc).__name__},
        )


async def ping_start(url: str, timeout: float) -> None:
    """POST {url}/start — call immediately before the sync begins."""
    await _post(f"{url}/start", timeout, label="start")


async def ping_success(url: str, timeout: float) -> None:
    """POST {url} — call after the sync completes successfully."""
    await _post(url, timeout, label="success")


async def ping_failure(url: str, timeout: float, *, body: str | None = None) -> None:
    """POST {url}/fail — call when the sync raises an exception.

    *body* is an optional short description of the failure (e.g. ``str(exc)``),
    trimmed to 10 000 bytes to stay within Healthchecks.io's payload limit.
    """
    trimmed: str | None = None
    if body:
        encoded = body.encode()[:10_000]
        trimmed = encoded.decode(errors="replace")
    await _post(f"{url}/fail", timeout, label="failure", body=trimmed)
