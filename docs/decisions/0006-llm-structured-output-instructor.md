# ADR 0006: LLM structured output — keep `instructor`, not LangChain

**Date:** 2026-06-15
**Status:** Accepted.

## Context

Adopting LangGraph nodes (ADR-0001) opens the option of LangChain's `ChatOpenAI(...).with_structured_output(PydanticModel)` for structured LLM output. The codebase already uses `instructor` (`instructor.from_openai` over `AsyncOpenAI`, JSON mode) against OpenRouter, with a proven retry/repair loop (`app/adapters/content/pure_summary_service.py::_summarize_with_instructor`).

## Decision

Keep `instructor` for structured output, called **inside** the graph nodes. Do **not** add `langchain-openai`.

## Rationale

- `instructor` is already proven against OpenRouter, including the tool-definition-drop edge case some OpenRouter models exhibit with tool-calling-based structured output.
- Fewer dependencies: `langchain-openai` pulls the `openai` SDK + `tiktoken`, and would introduce a second, parallel structured-output path for no benefit.
- The existing repair loop, model cascade, and `attempt_trigger` persistence stay intact.
- The main upside of `with_structured_output` (LangSmith tracing) is not used; we have OTel.

## Consequences

- Graph nodes wrap the existing OpenRouter structured-call path; model selection stays config-driven (`ratatoskr.yaml`, no code default — CLAUDE.md rule 11).
- Revisit only if we adopt LangChain LCEL chains broadly (then a single LLM abstraction may be worth the dependency).
