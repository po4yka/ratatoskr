"""Seed a small, representative dataset for the migration round-trip smoke test.

The CI migration round-trip upgrades to head, seeds this data, then downgrades
all the way to base. Seeding matters because several ``downgrade()`` functions
are data-dependent -- they recreate constraints, narrow column types, or alter
enums, and only fail when rows already exist. The canonical example is migration
0006, whose downgrade recreates a table-level ``UNIQUE(github_id)`` that breaks
the moment two users have starred the same repository (CLAUDE.md Operating Rule
#12; the 0006 downgrade was made conditional to survive exactly this case).

The seed deliberately covers:
- Two users sharing a duplicate ``github_id`` across two ``repositories`` rows --
  the documented 0006 data-dependent hotspot.
- A minimal core chain (chat -> request -> summary) so column-altering downgrades
  (e.g. widening/narrowing ``summaries.version``) run against real rows.

Run against a database already at Alembic head:

    DATABASE_URL=postgresql+asyncpg://... python -m tools.scripts.seed_migration_roundtrip
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.core import Chat, Request, Summary, User
from app.db.models.repository import Repository

_DUPLICATE_GITHUB_ID = 424242  # shared across two users -> exercises 0006 downgrade


async def _seed(dsn: str) -> None:
    engine = create_async_engine(dsn)
    try:
        session_maker = async_sessionmaker(engine, expire_on_commit=False)
        async with session_maker() as session, session.begin():
            owner = User(telegram_user_id=1001, username="owner", is_owner=True)
            second = User(telegram_user_id=1002, username="second", is_owner=False)
            session.add_all([owner, second])
            await session.flush()

            # Two repositories sharing github_id under different users. A downgrade
            # that recreates a table-level UNIQUE(github_id) fails here; a correct
            # (per-user) downgrade does not. This is the 0006 regression surface.
            session.add_all(
                [
                    Repository(
                        github_id=_DUPLICATE_GITHUB_ID,
                        owner="octocat",
                        name="hello-world",
                        full_name="octocat/hello-world",
                        url="https://github.com/octocat/hello-world",
                        user_id=owner.telegram_user_id,
                    ),
                    Repository(
                        github_id=_DUPLICATE_GITHUB_ID,
                        owner="octocat",
                        name="hello-world",
                        full_name="octocat/hello-world",
                        url="https://github.com/octocat/hello-world",
                        user_id=second.telegram_user_id,
                    ),
                ]
            )

            # Minimal core chain so column-altering downgrades run with data present.
            chat = Chat(chat_id=1001, type="private", title="seed-chat")
            session.add(chat)
            request = Request(type="url", correlation_id="seed-roundtrip-cid")
            session.add(request)
            await session.flush()
            session.add(Summary(request_id=request.id))
    finally:
        await engine.dispose()


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 64
    asyncio.run(_seed(dsn))
    print("migration round-trip seed inserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
