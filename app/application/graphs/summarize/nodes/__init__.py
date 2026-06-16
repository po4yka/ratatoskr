"""Summarize-graph node stubs (ADR-0015).

Each node is a plain ``async def node(state, *, deps) -> dict`` wrapped by
``@graph_node`` for its OTel span (see :mod:`._span`). Node bodies are langgraph-free
and depend only on the application ports in ``SummarizeDeps``; the full bodies land
in T6 (ground/build_prompt), T7 (extract/summarize/validate/repair/enrich/persist),
and T8 (notify/streaming).
"""

from __future__ import annotations

from app.application.graphs.summarize.nodes.build_prompt import build_prompt
from app.application.graphs.summarize.nodes.enrich import enrich
from app.application.graphs.summarize.nodes.extract import extract
from app.application.graphs.summarize.nodes.ground import ground
from app.application.graphs.summarize.nodes.ingest import ingest
from app.application.graphs.summarize.nodes.notify import notify
from app.application.graphs.summarize.nodes.persist import persist
from app.application.graphs.summarize.nodes.repair import repair
from app.application.graphs.summarize.nodes.summarize import summarize
from app.application.graphs.summarize.nodes.validate import validate

__all__ = [
    "build_prompt",
    "enrich",
    "extract",
    "ground",
    "ingest",
    "notify",
    "persist",
    "repair",
    "summarize",
    "validate",
]
