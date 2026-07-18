"""Reusable checkpoint-retention cleanup for the LangGraph Postgres schema.

The graph checkpointer and the scheduled prune task both use this transaction body. Keeping it here makes startup cleanup a prerequisite for exposing a durable saver, while preserving the scheduled task's separate connection and distributed lock.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

@dataclass
class CheckpointPruneStats:
    """Rows deleted from each checkpoint table."""

    checkpoints: int = 0
    checkpoint_blobs: int = 0
    checkpoint_writes: int = 0


async def prune_expired_checkpoints(
    connection: Any,
    *,
    schema: str,
    retention_days: int,
    cutoff: dt.datetime | None = None,
) -> CheckpointPruneStats:
    """Delete whole checkpoint runs whose latest checkpoint predates retention.

    ``connection`` must already be inside a transaction. LangGraph stores an ISO
    timestamp in the JSONB checkpoint payload. Aging the lineage from that payload
    covers ordinary request threads as well as deleted requests, changed
    correlation IDs, and content-only runs that never had a request row.
    """
    # psycopg belongs to the optional ``graph`` extra. Keep the infrastructure
    # package importable for workers that do not install checkpoint support.
    from psycopg import sql

    cutoff = cutoff or dt.datetime.now(dt.UTC) - dt.timedelta(days=retention_days)
    stats = CheckpointPruneStats()
    select_query = sql.SQL(
        "SELECT thread_id FROM {}.{} "
        "WHERE checkpoint->>'ts' IS NOT NULL "
        "GROUP BY thread_id "
        "HAVING MAX((checkpoint->>'ts')::timestamptz) < %(cutoff)s"
    ).format(sql.Identifier(schema), sql.Identifier("checkpoints"))
    cur = await connection.execute(select_query, {"cutoff": cutoff})
    thread_ids = [
        row["thread_id"] if isinstance(row, Mapping) else row[0]
        for row in await cur.fetchall()
    ]
    if not thread_ids:
        return stats

    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        # `schema` is validated [A-Za-z0-9_] at config time and `table` is a fixed
        # literal, so neither identifier is user-controlled; compose them via
        # psycopg.sql for defense in depth (this also keeps the query off bandit's
        # B608 string-construction heuristic without a suppression comment).
        query = sql.SQL("DELETE FROM {}.{} WHERE thread_id = ANY(%(ids)s)").format(
            sql.Identifier(schema), sql.Identifier(table)
        )
        del_cur = await connection.execute(query, {"ids": thread_ids})
        setattr(stats, table, del_cur.rowcount or 0)

    return stats
