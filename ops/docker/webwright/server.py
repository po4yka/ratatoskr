"""Webwright sidecar HTTP server.

Wraps Microsoft Webwright's agent loop behind a stable HTTP contract so
Ratatoskr can invoke it without owning the upstream repo's evolving Python
API. Two endpoints:

  POST /scrape  - article-style extraction for the scraper-chain provider
  POST /task    - free-form interactive task for the `/browse` command

Both endpoints honor an `X-Correlation-Id` header that must be preserved in
every log line and in the response so failures can be traced back to a
specific Ratatoskr request (Operating Rule 1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header
from pydantic import BaseModel, Field

logger = logging.getLogger("webwright_sidecar")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

OUTPUTS_DIR = Path(os.environ.get("WEBWRIGHT_OUTPUTS_DIR", "/data/webwright"))
DEFAULT_MODEL = os.environ.get("WEBWRIGHT_DEFAULT_MODEL", "openai/gpt-4o-mini")
DEFAULT_MAX_STEPS = int(os.environ.get("WEBWRIGHT_DEFAULT_MAX_STEPS", "20"))
DEFAULT_TIMEOUT_SEC = int(os.environ.get("WEBWRIGHT_DEFAULT_TIMEOUT_SEC", "180"))


app = FastAPI(title="Ratatoskr Webwright sidecar", version="0.1.0")


class ScrapeRequest(BaseModel):
    url: str = Field(description="Target URL to extract content from.")
    max_steps: int = Field(default=DEFAULT_MAX_STEPS, ge=1, le=100)
    timeout_sec: int = Field(default=DEFAULT_TIMEOUT_SEC, ge=10, le=600)
    model: str | None = Field(default=None, description="Optional model override.")


class TaskRequest(BaseModel):
    task: str = Field(description="Natural-language task for the agent.")
    allowed_domains: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=DEFAULT_MAX_STEPS, ge=1, le=100)
    timeout_sec: int = Field(default=DEFAULT_TIMEOUT_SEC, ge=10, le=600)
    model: str | None = None
    cookies_json: dict[str, Any] | None = Field(
        default=None,
        description="Per-domain cookie jar to inject before the run starts.",
    )


class ScrapeResponse(BaseModel):
    correlation_id: str
    status: str
    title: str | None = None
    body_markdown: str | None = None
    metadata: dict[str, Any] | None = None
    screenshots: list[str] = Field(default_factory=list)
    trajectory_path: str | None = None
    steps_used: int | None = None
    llm_cost_usd: float | None = None
    error_text: str | None = None
    latency_ms: int


class TaskResponse(BaseModel):
    correlation_id: str
    status: str
    final_answer: str | None = None
    screenshots: list[str] = Field(default_factory=list)
    trajectory_path: str | None = None
    steps_used: int | None = None
    llm_cost_usd: float | None = None
    error_text: str | None = None
    latency_ms: int


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(
    payload: ScrapeRequest,
    x_correlation_id: str | None = Header(default=None),
) -> ScrapeResponse:
    cid = x_correlation_id or f"wwr-{uuid.uuid4().hex[:12]}"
    started = time.perf_counter()
    logger.info(
        "webwright_scrape_received",
        extra={"cid": cid, "url": payload.url, "max_steps": payload.max_steps},
    )

    task = _build_extraction_task(payload.url)
    try:
        result = await _run_webwright_task(
            task=task,
            cid=cid,
            max_steps=payload.max_steps,
            timeout_sec=payload.timeout_sec,
            model=payload.model or DEFAULT_MODEL,
            allowed_domains=[_host_of(payload.url)],
            cookies_json=None,
        )
    except TimeoutError:
        latency = int((time.perf_counter() - started) * 1000)
        logger.warning("webwright_scrape_timeout", extra={"cid": cid, "url": payload.url})
        return ScrapeResponse(
            correlation_id=cid,
            status="timeout",
            error_text=f"Webwright timed out after {payload.timeout_sec}s",
            latency_ms=latency,
        )
    except WebwrightNotAvailableError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return ScrapeResponse(
            correlation_id=cid,
            status="error",
            error_text=str(exc),
            latency_ms=latency,
        )
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        logger.exception("webwright_scrape_failed", extra={"cid": cid})
        return ScrapeResponse(
            correlation_id=cid,
            status="error",
            error_text=f"Webwright failed: {exc}",
            latency_ms=latency,
        )

    latency = int((time.perf_counter() - started) * 1000)
    parsed = _parse_extraction_answer(result.final_answer)
    return ScrapeResponse(
        correlation_id=cid,
        status=result.status,
        title=parsed.get("title"),
        body_markdown=parsed.get("body_markdown") or result.final_answer,
        metadata=parsed.get("metadata"),
        screenshots=result.screenshots,
        trajectory_path=result.trajectory_path,
        steps_used=result.steps_used,
        llm_cost_usd=result.llm_cost_usd,
        latency_ms=latency,
    )


@app.post("/task", response_model=TaskResponse)
async def task(
    payload: TaskRequest,
    x_correlation_id: str | None = Header(default=None),
) -> TaskResponse:
    cid = x_correlation_id or f"wwr-{uuid.uuid4().hex[:12]}"
    started = time.perf_counter()
    logger.info(
        "webwright_task_received",
        extra={"cid": cid, "max_steps": payload.max_steps, "task_len": len(payload.task)},
    )

    try:
        result = await _run_webwright_task(
            task=payload.task,
            cid=cid,
            max_steps=payload.max_steps,
            timeout_sec=payload.timeout_sec,
            model=payload.model or DEFAULT_MODEL,
            allowed_domains=payload.allowed_domains,
            cookies_json=payload.cookies_json,
        )
    except TimeoutError:
        latency = int((time.perf_counter() - started) * 1000)
        return TaskResponse(
            correlation_id=cid,
            status="timeout",
            error_text=f"Webwright timed out after {payload.timeout_sec}s",
            latency_ms=latency,
        )
    except WebwrightNotAvailableError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return TaskResponse(
            correlation_id=cid,
            status="error",
            error_text=str(exc),
            latency_ms=latency,
        )
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        logger.exception("webwright_task_failed", extra={"cid": cid})
        return TaskResponse(
            correlation_id=cid,
            status="error",
            error_text=f"Webwright failed: {exc}",
            latency_ms=latency,
        )

    latency = int((time.perf_counter() - started) * 1000)
    return TaskResponse(
        correlation_id=cid,
        status=result.status,
        final_answer=result.final_answer,
        screenshots=result.screenshots,
        trajectory_path=result.trajectory_path,
        steps_used=result.steps_used,
        llm_cost_usd=result.llm_cost_usd,
        latency_ms=latency,
    )


# --------------------------------------------------------------------------- #
# Webwright invocation                                                        #
# --------------------------------------------------------------------------- #


class WebwrightNotAvailableError(RuntimeError):
    """Raised when the Webwright agent loop is not importable/invokable."""


class _AgentResult:
    __slots__ = (
        "final_answer",
        "llm_cost_usd",
        "screenshots",
        "status",
        "steps_used",
        "trajectory_path",
    )

    def __init__(
        self,
        *,
        status: str,
        final_answer: str | None,
        screenshots: list[str],
        trajectory_path: str | None,
        steps_used: int | None,
        llm_cost_usd: float | None,
    ) -> None:
        self.status = status
        self.final_answer = final_answer
        self.screenshots = screenshots
        self.trajectory_path = trajectory_path
        self.steps_used = steps_used
        self.llm_cost_usd = llm_cost_usd


def _build_extraction_task(url: str) -> str:
    return (
        "Visit the URL below, extract the main article content, then return a single "
        "JSON object with keys: title (string), body_markdown (string with the full "
        "article body as Markdown), metadata (object with author/published/lang if "
        "visible). Do not include navigation, ads, or unrelated boilerplate. If the "
        "page is behind a login or paywall and you have credentials available, log "
        "in first; otherwise report status=blocked in the JSON.\n\n"
        f"URL: {url}"
    )


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


async def _run_webwright_task(
    *,
    task: str,
    cid: str,
    max_steps: int,
    timeout_sec: int,
    model: str,
    allowed_domains: list[str],
    cookies_json: dict[str, Any] | None,
) -> _AgentResult:
    """Invoke Webwright's agent loop. This is the upstream-integration boundary.

    The implementation below shells out to the `webwright` CLI installed by the
    Dockerfile's editable install. It is intentionally narrow: stdin holds the
    task string, stdout/stderr are captured, and the report.json the agent
    writes under WEBWRIGHT_OUTPUTS_DIR/<task_id>/ is parsed for the structured
    result. Adjust this function (or the env knobs it reads) when upstream
    publishes a stable Python API.
    """

    run_dir = OUTPUTS_DIR / cid
    run_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.setdefault("OPENAI_BASE_URL", os.environ.get("OPENAI_BASE_URL", ""))
    env.setdefault("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    env["WEBWRIGHT_MODEL"] = model
    env["WEBWRIGHT_RUN_DIR"] = str(run_dir)
    env["WEBWRIGHT_MAX_STEPS"] = str(max_steps)
    env["WEBWRIGHT_CORRELATION_ID"] = cid
    if allowed_domains:
        env["WEBWRIGHT_ALLOWED_DOMAINS"] = ",".join(allowed_domains)
    if cookies_json:
        (run_dir / "cookies.json").write_text(json.dumps(cookies_json))
        env["WEBWRIGHT_COOKIES_FILE"] = str(run_dir / "cookies.json")

    # Probe the upstream binary at runtime — if it isn't on PATH the sidecar
    # still boots and reports a clean error, which is what tests rely on.
    if shutil.which("webwright") is None:
        raise WebwrightNotAvailableError(
            "Webwright CLI not found in PATH; sidecar image was built without the upstream binary."
        )

    started = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            "webwright",
            "run",
            "--task",
            task,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(run_dir),
            env=env,
        )
        try:
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
    except FileNotFoundError as exc:
        raise WebwrightNotAvailableError(
            "Webwright executable disappeared between probe and invocation"
        ) from exc

    logger.info(
        "webwright_run_completed",
        extra={
            "cid": cid,
            "elapsed_sec": time.perf_counter() - started,
            "returncode": proc.returncode,
        },
    )

    report_path = _find_latest_report(run_dir)
    if report_path is None:
        return _AgentResult(
            status="error",
            final_answer=(stdout_b.decode("utf-8", errors="replace") or None),
            screenshots=[],
            trajectory_path=str(run_dir),
            steps_used=None,
            llm_cost_usd=None,
        )

    try:
        report = json.loads(report_path.read_text())
    except Exception:
        report = {}

    status = "ok" if proc.returncode == 0 else "error"
    return _AgentResult(
        status=status,
        final_answer=report.get("final_answer")
        or stdout_b.decode("utf-8", errors="replace")
        or None,
        screenshots=[str(p) for p in run_dir.glob("*.png")],
        trajectory_path=str(report_path.parent),
        steps_used=report.get("steps_used"),
        llm_cost_usd=report.get("llm_cost_usd"),
    )


def _find_latest_report(run_dir: Path) -> Path | None:
    candidates = list(run_dir.rglob("report.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _parse_extraction_answer(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    text = raw.strip()
    # Webwright's final answer is normally the agent's last assistant message;
    # the extraction prompt asks for a JSON object, but the agent can wrap it
    # in markdown fences or prose. Be forgiving.
    if text.startswith("```"):
        end = text.find("```", 3)
        if end != -1:
            inner = text[3:end].strip()
            if inner.lower().startswith("json"):
                inner = inner[4:].strip()
            text = inner
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed
