# LLM Providers

Ratatoskr selects the summarization LLM adapter with `runtime.llm_provider` in `ratatoskr.yaml` or `LLM_PROVIDER` in the environment. Supported values are `openrouter`, `openai`, `anthropic`, and `ollama`.

OpenRouter remains the default and most feature-complete production path because it owns the fallback ladder, OpenRouter-specific usage metadata, provider routing, prompt-cache knobs, and structured-output downgrade behavior. The direct providers are intentionally narrow: they make one provider endpoint usable through the same `LLMClientProtocol` and are covered by mocked structured-output roundtrip tests.

| Provider | Adapter | Required settings | Structured JSON | Fallback models | Streaming | Prompt caching | Vision/multimodal |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `openrouter` | `app/adapters/openrouter/openrouter_client.py` | `OPENROUTER_API_KEY`, `openrouter.model`, `openrouter.fallback_models`, flash/long-context model settings | `json_schema` with `json_object` fallback, provider capability checks | Yes | Yes | OpenRouter/provider-specific knobs | Used by existing article/attachment vision paths |
| `openai` | `app/adapters/llm/openai_compatible.py` | `OPENAI_API_KEY`, `OPENAI_MODEL` | OpenAI-compatible `response_format={"type":"json_object"}` plus local Pydantic validation | No | No | Not wired | Text-only in this adapter |
| `anthropic` | `app/adapters/llm/anthropic_direct.py` | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Assistant-turn prefill (the Messages API has no `response_format`) plus local Pydantic validation | No | No | Not wired in the direct adapter | Text-only in this adapter |
| `ollama` | `app/adapters/llm/openai_compatible.py` | `OLLAMA_MODEL` | OpenAI-compatible `response_format={"type":"json_object"}` plus local Pydantic validation | No | No | Not applicable | Text-only in this adapter |

## Retry Policy

Both direct adapters share one loop, `BaseLLMClient._run_structured_attempts`. Keeping it in the base class is deliberate: the adapters used to carry their own copies and the copies drifted.

- **Retried:** transport faults (timeout, connection reset), `429`, all `5xx`, and a reply that is not usable JSON.
- **Not retried:** `400`, `401`, `402`, `403`, `404`, `405`, `410`, `413`, `422`. A rotated key stays rotated, so a retry only doubles the bill.
- **Pacing:** exponential backoff with jitter between attempts, never after the last one. A `Retry-After` response header takes precedence and is capped at 120 s so a bad or hostile value cannot park the summarize graph.
- **Budget:** the caller's `max_retries` (`SUMMARIZATION_MAX_RETRIES`) bounded by the provider's own `OPENAI_MAX_RETRIES` / `ANTHROPIC_MAX_RETRIES` / `OLLAMA_MAX_RETRIES`. Lower the provider value to stop a slow local model from being hammered.
- **Telemetry:** every attempt is one billed provider request and produces one `physical_attempts` row, so `llm_calls` records the retries as well as the winner. The rows are attached to the terminal exception too, because a failed call is still a call that happened (Operating Rule 3).

## Cost Accounting

Direct providers do not report a USD cost the way OpenRouter does. Set `OPENAI_PRICE_INPUT_PER_1K` / `OPENAI_PRICE_OUTPUT_PER_1K` (and the `ANTHROPIC_*` / `OLLAMA_*` equivalents) to price calls from token counts, mirroring the existing `OPENROUTER_PRICE_*_PER_1K` overrides.

Leaving them unset writes `NULL` costs. `LLM_DAILY_HARD_BUDGET_USD` and `LLM_MONTHLY_HARD_BUDGET_USD` sum `llm_calls.cost_usd`, so an unpriced provider makes those hard stops unreachable.

## Operational Recommendations

- Use `openrouter` when you need the mature summarization path: fallback models, OpenRouter model-family routing, provider-order controls, prompt-cache accounting, streaming, and the existing vision-related model wiring.
- Use `openai` when you want direct OpenAI billing, lower proxy surface area, or residency/compliance constraints that forbid OpenRouter as an intermediary.
- Use `anthropic` when you want a direct Anthropic key and direct Messages API behavior. The direct adapter does not yet expose Anthropic prompt-cache controls, so `anthropic` direct mode is not a drop-in replacement for all OpenRouter Anthropic cache workflows.
- Use `ollama` for local or LAN-hosted OpenAI-compatible inference. Expect weaker JSON adherence with many local models; keep model-specific smoke tests in place before relying on it for unattended summaries.
- Keep provider-specific API keys in `.env`; non-secret model names, base URLs, and timeouts can live in `ratatoskr.yaml`.

## Test Coverage

- Direct provider roundtrips: `tests/adapters/llm/test_direct_provider_e2e.py`.
- Retry policy, backoff, and per-attempt telemetry: `tests/adapters/llm/test_direct_adapter_retry_policy.py`.
- Provider selection/factory dispatch: `tests/config/test_llm_provider_selection.py`.
- OpenRouter behavior: existing OpenRouter-focused tests under `tests/test_openrouter_*.py`, `tests/unit/llm/`, and related summarization workflow tests.
