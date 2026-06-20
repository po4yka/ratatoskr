"""Seed a local development database with a demo user and summaries."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
from app.core.url_utils import compute_dedupe_hash, normalize_url
from app.db.models import CrawlResult, Request, Summary, User
from app.db.session import Database

DEFAULT_DEV_USER_ID = 424242
DEFAULT_DEMO_COUNT = 10


@dataclass(frozen=True)
class DemoSummarySeed:
    title: str
    url: str
    source_type: str
    tags: tuple[str, ...]
    tldr: str
    summary_250: str
    summary_1000: str


DEMO_SUMMARIES: tuple[DemoSummarySeed, ...] = (
    DemoSummarySeed(
        title="Tracing a Production Incident",
        url="https://example.com/demo/tracing-production-incident",
        source_type="article",
        tags=("observability", "ops"),
        tldr="Correlation IDs, spans, and persisted payloads turn a vague report into a short debug path.",
        summary_250="A production debugging walkthrough shows how request IDs, structured logs, database rows, and OpenTelemetry spans fit together when a summary fails.",
        summary_1000="The article follows a failed content-ingestion report from the user-visible Error ID through logs, persisted request rows, scraper attempts, LLM calls, and the final summary record. It emphasizes keeping diagnostics searchable and linking timing data with payload persistence.",
    ),
    DemoSummarySeed(
        title="Designing a Reliable Scraper Chain",
        url="https://example.com/demo/reliable-scraper-chain",
        source_type="article",
        tags=("scraper", "reliability"),
        tldr="Provider fallback works best when every rung records an outcome and a latency budget.",
        summary_250="A scraper-chain design note compares direct HTTP, readability extraction, browser providers, and sidecar services, with attention to cancellation and per-provider telemetry.",
        summary_1000="The note explains why a single extractor is brittle for modern pages and how tiered fallback can improve success rates. It recommends explicit provider ordering, bounded timeouts, SSRF checks, attempt logs, and final-result quality checks before a page reaches summarization.",
    ),
    DemoSummarySeed(
        title="Mobile Sync Conflict Basics",
        url="https://example.com/demo/mobile-sync-conflicts",
        source_type="article",
        tags=("mobile", "sync"),
        tldr="A server-version cursor is only useful if each write updates the same ordering contract.",
        summary_250="A mobile sync primer covers pull cursors, tombstones, conflict visibility, and why pagination metadata must describe only the page actually returned.",
        summary_1000="The guide sketches a small library sync protocol for summaries, collections, tags, and read state. It highlights monotonic server versions, explicit deletes, deterministic conflict handling, and contract tests that exercise multi-page sessions.",
    ),
    DemoSummarySeed(
        title="Vector Index Reconciliation",
        url="https://example.com/demo/vector-reconciliation",
        source_type="article",
        tags=("qdrant", "search"),
        tldr="Fast-path vector writes need a reconciler because every external store eventually drifts.",
        summary_250="A vector-search operations note describes deterministic point IDs, reconciliation scans, and metrics that detect mismatches between PostgreSQL and Qdrant.",
        summary_1000="The write-up treats PostgreSQL as the system of record and Qdrant as a derived index. It explains how deterministic point IDs, pending index status rows, retryable repair jobs, and alertable drift counters keep semantic search trustworthy.",
    ),
    DemoSummarySeed(
        title="Repository Analysis with Structured Output",
        url="https://example.com/demo/repository-analysis-structured-output",
        source_type="github",
        tags=("github", "llm"),
        tldr="Repository summaries are easier to search when the LLM returns a schema, not prose.",
        summary_250="A repository-ingestion walkthrough turns README, topics, languages, stars, and metadata into a typed analysis object suitable for search and recommendation.",
        summary_1000="The article explains a repository analysis pipeline that gathers GitHub metadata, builds a bounded prompt, asks a structured-output model for purpose and quality signals, persists the result, and refreshes repository embeddings.",
    ),
    DemoSummarySeed(
        title="Owner-Only Auth for Self-Hosted Tools",
        url="https://example.com/demo/owner-only-auth",
        source_type="article",
        tags=("auth", "security"),
        tldr="A self-hosted single-user app should still fail closed when identity config is missing.",
        summary_250="An auth hardening note discusses Telegram allowlists, JWT required claims, refresh-token rotation, and why empty allowlists should not become implicit public access.",
        summary_1000="The post compares Telegram bot access, web login, mobile JWT sessions, and MCP exposure modes. It recommends required audience and issuer claims, per-client rate limits, token-family revocation, secure cookies, and explicit allowlist checks.",
    ),
    DemoSummarySeed(
        title="Running Background Jobs Without Losing Work",
        url="https://example.com/demo/background-job-reliability",
        source_type="article",
        tags=("taskiq", "jobs"),
        tldr="Retries need leases, idempotency keys, and a place for permanent failures to land.",
        summary_250="A background-worker reliability guide covers durable queues, startup reconciliation, retry middleware, dead-letter rows, and operational alerts for stuck jobs.",
        summary_1000="The guide follows a URL-processing job through enqueue, lease acquisition, extraction, summarization, persistence, and notification. It highlights duplicate suppression, cancellation safety, retry-after handling, and DLQ inspection commands.",
    ),
    DemoSummarySeed(
        title="Digest Pipelines and Delivery Sinks",
        url="https://example.com/demo/digest-delivery",
        source_type="telegram_digest",
        tags=("digest", "delivery"),
        tldr="Digests need separate metrics for collection, analysis, rendering, and delivery.",
        summary_250="A channel-digest design article explains how subscribed Telegram channels become scheduled recaps with delivery tracking and per-sink observability.",
        summary_1000="The article separates channel post collection, per-post analysis, digest synthesis, user preferences, and final delivery. It recommends counters for skipped posts, failed LLM analysis, rendered digest size, and delivery sink outcomes.",
    ),
    DemoSummarySeed(
        title="Public Sharing Without Data Leaks",
        url="https://example.com/demo/public-sharing",
        source_type="article",
        tags=("sharing", "privacy"),
        tldr="Share links should expose exactly the selected collection, not the owner library.",
        summary_250="A privacy-focused sharing note covers public collection links, token scope, revocation, RSS exports, and avoiding accidental multi-user data exposure.",
        summary_1000="The note describes a share-by-link feature for a personal archive. It recommends opaque tokens, collection-scoped queries, no owner-only metadata in responses, explicit revocation, and contract tests for unauthenticated access.",
    ),
    DemoSummarySeed(
        title="Local Contributor Onboarding",
        url="https://example.com/demo/local-contributor-onboarding",
        source_type="article",
        tags=("developer-experience", "ops"),
        tldr="A good bootstrap command starts dependencies, applies schema, seeds data, and prints the next command.",
        summary_250="A developer-experience checklist argues that a new contributor should reach a non-empty local UI with one command and no production secrets.",
        summary_1000="The checklist covers local service defaults, loopback-only ports, deterministic demo users, idempotent seed data, teardown commands, and documentation that names exactly where to go next.",
    ),
)


def _resolve_dsn(args: argparse.Namespace) -> str:
    if args.database_url:
        return args.database_url
    env_dsn = os.getenv("DATABASE_URL", "").strip()
    if env_dsn:
        return env_dsn
    password = os.getenv("POSTGRES_PASSWORD", "").strip()
    if password:
        return f"postgresql+asyncpg://ratatoskr_app:{password}@127.0.0.1:5432/ratatoskr"
    msg = "DATABASE_URL or POSTGRES_PASSWORD is required"
    raise SystemExit(msg)


def _summary_payload(seed: DemoSummarySeed) -> dict[str, Any]:
    return {
        "title": seed.title,
        "summary_250": seed.summary_250,
        "summary_1000": seed.summary_1000,
        "tldr": seed.tldr,
        "key_ideas": [
            "Use deterministic local defaults for repeatable development.",
            "Keep production secrets out of demo seed data.",
            "Prefer idempotent setup steps so reruns are safe.",
        ],
        "topic_tags": list(seed.tags),
        "entities": [],
        "source_type": seed.source_type,
        "estimated_reading_time_min": 4,
    }


async def _upsert_user(session: Any, *, user_id: int) -> None:
    stmt = (
        pg_insert(User)
        .values(
            telegram_user_id=user_id,
            username="ratatoskr_demo",
            display_name="Ratatoskr Demo User",
            is_owner=True,
            locale="en",
            theme="dark",
            default_summary_language="auto",
            preferences_json={"seeded_by": "app.cli.seed_demo_data"},
            onboarding_completed_at=dt.datetime.now(UTC),
        )
        .on_conflict_do_update(
            index_elements=[User.telegram_user_id],
            set_={
                "username": "ratatoskr_demo",
                "display_name": "Ratatoskr Demo User",
                "is_owner": True,
                "onboarding_completed_at": dt.datetime.now(UTC),
            },
        )
    )
    await session.execute(stmt)


async def _upsert_demo_summary(session: Any, *, user_id: int, seed: DemoSummarySeed) -> int:
    normalized_url = normalize_url(seed.url)
    dedupe_hash = compute_dedupe_hash(normalized_url)
    existing_request_id = await session.scalar(
        select(Request.id).where(Request.user_id == user_id, Request.dedupe_hash == dedupe_hash)
    )
    if existing_request_id is None:
        request = Request(
            type="url",
            status="completed",
            correlation_id=f"demo-{dedupe_hash[:12]}",
            user_id=user_id,
            input_url=seed.url,
            normalized_url=normalized_url,
            dedupe_hash=dedupe_hash,
            content_text=seed.summary_1000,
            processing_time_ms=1200,
        )
        session.add(request)
        await session.flush()
        request_id = int(request.id)
    else:
        request_id = int(existing_request_id)
        await session.execute(
            update(Request)
            .where(Request.id == request_id, Request.user_id == user_id)
            .values(
                status="completed",
                input_url=seed.url,
                normalized_url=normalized_url,
                content_text=seed.summary_1000,
                processing_time_ms=1200,
            )
        )

    crawl_stmt = (
        pg_insert(CrawlResult)
        .values(
            request_id=request_id,
            source_url=normalized_url,
            endpoint="demo_seed",
            status="success",
            http_status=200,
            content_markdown=f"# {seed.title}\n\n{seed.summary_1000}",
            metadata_json={"source_type": seed.source_type, "seeded": True},
            raw_response_json={"provider": "demo_seed"},
            latency_ms=75,
            firecrawl_success=True,
            correlation_id=f"demo-{dedupe_hash[:12]}",
            winning_provider="demo_seed",
        )
        .on_conflict_do_update(
            index_elements=[CrawlResult.request_id],
            set_={
                "source_url": normalized_url,
                "endpoint": "demo_seed",
                "status": "success",
                "content_markdown": f"# {seed.title}\n\n{seed.summary_1000}",
                "metadata_json": {"source_type": seed.source_type, "seeded": True},
                "latency_ms": 75,
                "firecrawl_success": True,
                "winning_provider": "demo_seed",
            },
        )
    )
    await session.execute(crawl_stmt)

    payload = _summary_payload(seed)
    summary_stmt = (
        pg_insert(Summary)
        .values(
            request_id=request_id,
            lang="en",
            json_payload=payload,
            insights_json={"seeded": True},
            title=seed.title,
            source_type=seed.source_type,
            reading_time=4,
            topic_tags=list(seed.tags),
            is_read=False,
            is_favorited=False,
        )
        .on_conflict_do_update(
            index_elements=[Summary.request_id],
            set_={
                "lang": "en",
                "json_payload": payload,
                "insights_json": {"seeded": True},
                "title": seed.title,
                "source_type": seed.source_type,
                "reading_time": 4,
                "topic_tags": list(seed.tags),
                "is_deleted": False,
            },
        )
        .returning(Summary.id)
    )
    summary_id = await session.scalar(summary_stmt)
    return int(summary_id)


async def seed_demo_data(args: argparse.Namespace) -> int:
    dsn = _resolve_dsn(args)
    db = Database(DatabaseConfig(dsn=dsn))
    try:
        seeds = DEMO_SUMMARIES[: args.count]
        async with db.transaction() as session:
            await _upsert_user(session, user_id=args.user_id)
            summary_ids = [
                await _upsert_demo_summary(session, user_id=args.user_id, seed=seed)
                for seed in seeds
            ]
        print(f"Seeded demo user {args.user_id} and {len(summary_ids)} summaries.")
        print(f"Set ALLOWED_USER_IDS={args.user_id} when running the bot/API locally.")
        return 0
    finally:
        await db.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="", help="SQLAlchemy asyncpg DSN")
    parser.add_argument("--user-id", type=int, default=DEFAULT_DEV_USER_ID)
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_DEMO_COUNT,
        choices=range(1, len(DEMO_SUMMARIES) + 1),
        metavar=f"1..{len(DEMO_SUMMARIES)}",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(seed_demo_data(args))


if __name__ == "__main__":
    raise SystemExit(main())
