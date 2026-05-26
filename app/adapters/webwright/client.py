"""Async HTTP client for the Webwright sidecar.

Used by the ``/browse`` Telegram command (Path B) and by the OpenRouter tool
hook (Path C). The scraper-chain provider has its own POST /scrape path; this
client owns POST /task, the free-form interactive surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.logging_utils import get_logger
from app.security.ssrf import make_safe_async_client

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SEC = 180
_DEFAULT_URL = "http://webwright:8090"


@dataclass(frozen=True)
class WebwrightTaskResult:
    """Structured result of a single ``/task`` invocation."""

    status: str
    final_answer: str | None
    screenshots: tuple[str, ...]
    trajectory_path: str | None
    steps_used: int | None
    llm_cost_usd: float | None
    error_text: str | None
    latency_ms: int
    correlation_id: str | None


class WebwrightClient:
    """Thin async wrapper over the sidecar's HTTP contract."""

    def __init__(
        self,
        *,
        url: str = _DEFAULT_URL,
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
        default_model: str | None = None,
        default_max_steps: int = 20,
    ) -> None:
        self._url = url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._default_model = default_model
        self._default_max_steps = default_max_steps

    async def run_task(
        self,
        *,
        task: str,
        correlation_id: str | None = None,
        allowed_domains: tuple[str, ...] | list[str] = (),
        cookies_json: dict[str, Any] | None = None,
        max_steps: int | None = None,
        timeout_sec: int | None = None,
        model: str | None = None,
    ) -> WebwrightTaskResult:
        """Invoke POST /task on the sidecar.

        Network errors surface as ``WebwrightTaskResult(status="error", ...)``
        so callers can persist failures alongside successes (Operating Rule 3).
        """

        body: dict[str, Any] = {
            "task": task,
            "allowed_domains": list(allowed_domains),
            "max_steps": max_steps or self._default_max_steps,
            "timeout_sec": timeout_sec or self._timeout_sec,
        }
        chosen_model = model or self._default_model
        if chosen_model:
            body["model"] = chosen_model
        if cookies_json:
            body["cookies_json"] = cookies_json

        headers: dict[str, str] = {"Accept": "application/json"}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id

        endpoint = f"{self._url}/task"
        # Client-side timeout pads the sidecar's wall clock so we get its
        # structured timeout response instead of httpx aborting first.
        client_timeout = (timeout_sec or self._timeout_sec) + 10

        try:
            async with make_safe_async_client(timeout=client_timeout) as client:
                response = await client.post(endpoint, json=body, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException:
            logger.warning(
                "webwright_client_timeout",
                extra={"cid": correlation_id, "timeout_sec": client_timeout},
            )
            return WebwrightTaskResult(
                status="error",
                final_answer=None,
                screenshots=(),
                trajectory_path=None,
                steps_used=None,
                llm_cost_usd=None,
                error_text=f"Webwright client timeout after {client_timeout}s",
                latency_ms=client_timeout * 1000,
                correlation_id=correlation_id,
            )
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "webwright_client_http_error",
                extra={"cid": correlation_id, "status_code": exc.response.status_code},
            )
            return WebwrightTaskResult(
                status="error",
                final_answer=None,
                screenshots=(),
                trajectory_path=None,
                steps_used=None,
                llm_cost_usd=None,
                error_text=f"Webwright HTTP {exc.response.status_code}",
                latency_ms=0,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            logger.warning(
                "webwright_client_failed",
                extra={
                    "cid": correlation_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return WebwrightTaskResult(
                status="error",
                final_answer=None,
                screenshots=(),
                trajectory_path=None,
                steps_used=None,
                llm_cost_usd=None,
                error_text=f"Webwright client failed: {exc}",
                latency_ms=0,
                correlation_id=correlation_id,
            )

        if not isinstance(data, dict):
            return WebwrightTaskResult(
                status="error",
                final_answer=None,
                screenshots=(),
                trajectory_path=None,
                steps_used=None,
                llm_cost_usd=None,
                error_text=f"Webwright returned non-object payload: {type(data).__name__}",
                latency_ms=0,
                correlation_id=correlation_id,
            )

        screenshots_raw = data.get("screenshots") or []
        screenshots = (
            tuple(str(s) for s in screenshots_raw) if isinstance(screenshots_raw, list) else ()
        )

        return WebwrightTaskResult(
            status=str(data.get("status") or "error"),
            final_answer=data.get("final_answer"),
            screenshots=screenshots,
            trajectory_path=data.get("trajectory_path"),
            steps_used=_coerce_int(data.get("steps_used")),
            llm_cost_usd=_coerce_float(data.get("llm_cost_usd")),
            error_text=data.get("error_text"),
            latency_ms=int(data.get("latency_ms") or 0),
            correlation_id=data.get("correlation_id") or correlation_id,
        )


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
