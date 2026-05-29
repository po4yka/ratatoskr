"""Re-export shim — implementation relocated to ``app.core.llm_call_budget``.

The original module was placed here by the perf agent but belongs in ``app.core``
(dependency-free, used by the application layer without an adapters import).
Adapter-side callers continue to work unchanged via this shim.
"""

from __future__ import annotations

from app.core.llm_call_budget import LLMCallBudget, LLMCallCapExceeded

__all__ = ["LLMCallBudget", "LLMCallCapExceeded"]
