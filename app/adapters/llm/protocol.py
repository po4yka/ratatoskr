"""LLM client protocol — re-exported from the application port.

The canonical definition lives in ``app.application.ports.llm_client`` so that
the application layer can depend on it without importing from ``app.adapters``.
This module re-exports the Protocol unchanged so that adapter-side code that
already imports from ``app.adapters.llm.protocol`` continues to work.
"""

from __future__ import annotations

from app.application.ports.llm_client import LLMClientProtocol

__all__ = ["LLMClientProtocol"]
