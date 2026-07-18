"""LangGraph checkpoint persistence configuration.

Drives the dedicated psycopg3 ``AsyncConnectionPool`` + ``AsyncPostgresSaver``
that persist LangGraph summarize-graph state between nodes (ADR-0004). This pool
is the **only** sanctioned non-``Database`` Postgres connection in the process
(invariant 4, ADR-0018) and is psycopg3, not asyncpg — ``langgraph-checkpoint-postgres``
requires psycopg3 and cannot route through ``app.db.session.Database``.

Everything here is gated OFF by default (``LANGGRAPH_CHECKPOINT_ENABLED=false``);
nothing opens a pool or creates a schema until a deployment opts in (strangler-fig,
ADR-0018 / ADR-0013).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LangGraphCheckpointConfig(BaseModel):
    """Configuration for the LangGraph Postgres checkpointer (ADR-0004)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=False,
        validation_alias="LANGGRAPH_CHECKPOINT_ENABLED",
        description=(
            "Master switch for the LangGraph Postgres checkpointer. When false "
            "(default) no psycopg3 pool is opened, no `langgraph` schema is "
            "created, and the prune job early-returns. Gated off until the graph "
            "cutover (ADR-0013)."
        ),
    )
    strict_msgpack: bool = Field(
        default=True,
        validation_alias="LANGGRAPH_STRICT_MSGPACK",
        description=(
            "When true (default) the checkpoint serializer disables the pickle "
            "fallback, so checkpoint blobs never trigger arbitrary-module "
            "deserialization (ADR-0004 security posture)."
        ),
    )
    schema_name: str = Field(
        default="langgraph",
        validation_alias="LANGGRAPH_CHECKPOINT_SCHEMA",
        description=(
            "Dedicated Postgres schema for the checkpoint tables (checkpoints, "
            "checkpoint_blobs, checkpoint_writes, checkpoint_migrations). Created "
            "by AsyncPostgresSaver.setup() via search_path, NOT Alembic-managed; "
            "droppable to reset graph state."
        ),
    )
    pool_min_size: int = Field(
        default=1,
        ge=1,
        validation_alias="LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE",
        description="Minimum size of the dedicated psycopg3 checkpointer pool (ADR-0004: 1).",
    )
    pool_max_size: int = Field(
        default=5,
        ge=1,
        validation_alias="LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE",
        description=(
            "Maximum size of the dedicated psycopg3 checkpointer pool. ADR-0004 "
            "specifies 5 (the authoritative value for THIS pool). "
            "Counts against the Postgres connection budget."
        ),
    )
    dsn_override: str | None = Field(
        default=None,
        validation_alias="LANGGRAPH_CHECKPOINT_DSN",
        description=(
            "Optional psycopg3 DSN override. When None (default) the checkpointer "
            "derives its DSN from DATABASE_URL, stripping the '+asyncpg' driver "
            "suffix (psycopg3 uses the bare 'postgresql://' scheme)."
        ),
    )
    retention_days: int = Field(
        default=90,
        ge=1,
        validation_alias="LANGGRAPH_CHECKPOINT_RETENTION_DAYS",
        description=(
            "Age in days past which a run's checkpoint rows are pruned before "
            "the durable saver starts and by the nightly prune job. Aligned with "
            "the AuditLog 90-day ceiling "
            "(auth memo Decision 3 / ADR-0004)."
        ),
    )
    prune_cron: str = Field(
        default="30 4 * * *",
        validation_alias="LANGGRAPH_CHECKPOINT_PRUNE_CRON",
        description=(
            "UTC cron expression for the nightly checkpoint prune job. Default "
            "offset from the git-backup sync (0 4 * * *) to avoid overlap."
        ),
    )

    @field_validator("schema_name", mode="before")
    @classmethod
    def _validate_schema_name(cls, value: Any) -> str:
        if value in (None, ""):
            return "langgraph"
        name = str(value).strip()
        # Defence-in-depth: the schema name is interpolated into a CREATE SCHEMA /
        # search_path statement, so restrict it to a safe identifier.
        if not name.replace("_", "").isalnum():
            msg = f"LANGGRAPH_CHECKPOINT_SCHEMA must be alphanumeric/underscore, got {name!r}"
            raise ValueError(msg)
        return name

    @field_validator("dsn_override", mode="before")
    @classmethod
    def _validate_dsn_override(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value).strip() or None

    @field_validator("prune_cron", mode="before")
    @classmethod
    def _validate_prune_cron(cls, value: Any) -> str:
        if value in (None, ""):
            return "30 4 * * *"
        cron = str(value).strip()
        if len(cron.split()) != 5:
            msg = "LANGGRAPH_CHECKPOINT_PRUNE_CRON must be a 5-field cron expression"
            raise ValueError(msg)
        return cron
