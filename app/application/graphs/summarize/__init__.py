"""Summarize StateGraph package (ADR-0010/0011/0015).

Public surface:

- :mod:`state` -- ``SummarizeState`` (serializable, id-based checkpoint state).
- :mod:`deps` -- ``SummarizeDeps`` (port-typed node dependency bundle).
- :mod:`graph` -- ``build_summarize_graph`` / ``run_summarize_graph`` (the ONLY
  langgraph-coupled surface, alongside ``app/di/graphs.py``).
- :mod:`runner` -- ``maybe_run_summarize_graph`` (flag-gated entrypoint).
- :mod:`lifecycle` -- the single terminal-failure path.
- :mod:`nodes` -- per-node ``async def(state, *, deps) -> dict`` stubs.
"""
