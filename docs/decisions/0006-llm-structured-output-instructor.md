# ADR 0006: LLM structured output — keep `instructor`, not LangChain

**Date:** 2026-06-15
**Status:** Accepted.

## Context

Adopting LangGraph nodes (ADR-0001) opened the option of LangChain's `ChatOpenAI(...).with_structured_output(PydanticModel)` for structured LLM output. The implemented graph instead calls `instructor` through `app/application/services/summarization/graph_llm.py::summarize_with_instructor`; `app/adapters/openrouter/openrouter_client.py` lazily wraps `AsyncOpenAI` with `instructor.from_openai(..., mode=instructor.Mode.JSON)`.

## Decision

Keep `instructor` for structured output, called **inside** the graph nodes. Do **not** add `langchain-openai`.

## Rationale

- `instructor` is already proven against OpenRouter, including the tool-definition-drop edge case some OpenRouter models exhibit with tool-calling-based structured output.
- Fewer dependencies: `langchain-openai` pulls the `openai` SDK + `tiktoken`, and would introduce a second, parallel structured-output path for no benefit.
- The graph-owned repair loop, model cascade, and `attempt_trigger` persistence stay intact.
- The main upside of `with_structured_output` (LangSmith tracing) is not used; we have OTel.

## Consequences

- Graph nodes wrap the existing OpenRouter structured-call path; model selection stays config-driven (`ratatoskr.yaml`, no code default — CLAUDE.md rule 11).
- Revisit only if we adopt LangChain LCEL chains broadly (then a single LLM abstraction may be worth the dependency).
