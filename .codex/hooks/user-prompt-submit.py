#!/usr/bin/env python3
"""Add Ratatoskr context for common Codex prompt topics."""

from __future__ import annotations

import json
import sys


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    prompt = str(data.get("prompt", "")).lower()
    context: list[str] = []

    if any(term in prompt for term in ("correlation", "correlation_id", "error id")):
        context.append(
            "CONTEXT: User-visible errors must include `Error ID: <correlation_id>`. "
            "Trace request state through `request_processing_jobs`, `crawl_results`, `llm_calls`, and summaries."
        )

    if any(term in prompt for term in ("database", "postgres", "query")):
        context.append(
            "CONTEXT: Ratatoskr uses PostgreSQL through `app/db/session.py`; use the inspecting-database skill for query patterns."
        )

    if "summary" in prompt and any(term in prompt for term in ("validate", "validation", "check", "contract")):
        context.append(
            "CONTEXT: Summary contracts live in `app/core/summary_contract.py`; use the validating-summaries skill."
        )

    if any(term in prompt for term in ("firecrawl", "openrouter", "api")):
        context.append(
            "CONTEXT: API integration debugging starts in `app/adapters/content/` and `app/adapters/openrouter/`; use the debugging-apis skill."
        )

    if any(term in prompt for term in ("frontend", "web", "react", "vite", "/web")):
        context.append(
            "CONTEXT: Editable React + TypeScript + Vite source lives in the external `ratatoskr-web` repository; this repo owns FastAPI `/web` serving and compiled `app/static/web/` assets. Use the web-frontend-dev skill."
        )

    if any(term in prompt for term in ("test", "cli")):
        context.append(
            "CONTEXT: Use `python -m app.cli.summary --url <URL>` for summary CLI checks and the testing-workflows skill for bot workflows."
        )

    if context:
        print("\n".join(context))


if __name__ == "__main__":
    main()
