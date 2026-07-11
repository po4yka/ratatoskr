"""Reusable checkpoint-retention cleanup for the LangGraph Postgres schema.

The graph checkpointer and the scheduled prune task both use this transaction body. Keeping it here makes startup cleanup a prerequisite for exposing a durable saver, while preserving the scheduled task's separate connection and distributed lock.
"""

from __future__ import annotations

import datetime as dt
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
    """Delete whole checkpoint runs whose parent request predates retention.

    ``connection`` must already be inside a transaction. The checkpoint tables have no timestamp column, so request creation time identifies aged runs via their shared ``thread_id == correlation_id`` contract.
    """
    cutoff = cutoff or dt.datetime.now(dt.UTC) - dt.timedelta(days=retention_days)
    stats = CheckpointPruneStats()
    cur = await connection.execute(
        "SELECT correlation_id FROM public.requests "
        "WHERE correlation_id IS NOT NULL AND created_at < %(cutoff)s",
        {"cutoff": cutoff},
    )
    thread_ids = [row[0] for row in await cur.fetchall()]
    if not thread_ids:
        return stats

    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        del_cur = await connection.execute(
            # `schema` is validated [A-Za-z0-9_] at config time and `table` is a
            # fixed literal, so neither identifier is user-controlled.
            f'DELETE FROM "{schema}".{table} WHERE thread_id = ANY(%(ids)s)',  # nosec B608
            {"ids": thread_ids},
        )
        setattr(stats, table, del_cur.rowcount or 0)

    return stats
