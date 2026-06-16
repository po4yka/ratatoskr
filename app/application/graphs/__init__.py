"""Application-layer LangGraph orchestration graphs (ADR-0010).

Graphs live in the application layer so their nodes depend only on application
ports (``application-no-outward`` stays green). The langgraph framework itself
is confined to each graph's assembly module (``graph.py``) and the DI
composition root (``app/di/graphs.py``); node bodies are plain
``async def(state, *, deps) -> dict`` and never import langgraph.
"""
