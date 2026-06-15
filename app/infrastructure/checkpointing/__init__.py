"""LangGraph Postgres checkpointer infrastructure (ADR-0004).

A dedicated psycopg3 pool + AsyncPostgresSaver, isolated from the asyncpg
``Database`` (invariant 4, ADR-0018). Gated off by default.
"""

from app.infrastructure.checkpointing.runtime import CheckpointerRuntime

__all__ = ["CheckpointerRuntime"]
