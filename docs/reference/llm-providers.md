# LLM Providers

Ratatoskr selects the summarization LLM adapter with `runtime.llm_provider` in `ratatoskr.yaml` or `LLM_PROVIDER` in the environment. Supported values are `openrouter`, `openai`, `anthropic`, and `ollama`.

OpenRouter remains the default and most feature-complete production path because it owns the fallback ladder, OpenRouter-specific usage metadata, provider routing, prompt-cache knobs, and structured-output downgrade behavior. The direct providers are intentionally narrow: they make one provider endpoint usable through the same `LLMClientProtocol` and are covered by mocked structured-output roundtrip tests.

| Provider | Adapter | Required settings | Structured JSON | Fallback models | Streaming | Prompt caching | Vision/multimodal |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `openrouter` | `app/adapters/openrouter/openrouter_client.py` | `OPENROUTER_API_KEY`, `openrouter.model`, `openrouter.fallback_models`, flash/long-context model settings | `json_schema` with `json_object` fallback, provider capability checks | Yes | Yes | OpenRouter/provider-specific knobs | Used by existing article/attachment vision paths |
| `openai` | `app/adapters/llm/openai_compatible.py` | `OPENAI_API_KEY`, `OPENAI_MODEL` | OpenAI-compatible `response_format={"type":"json_object"}` plus local Pydantic validation | No | No | Not wired | Text-only in this adapter |
| `anthropic` | `app/adapters/llm/anthropic_direct.py` | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Local Pydantic validation of returned JSON text | No | No | Not wired in the direct adapter | Text-only in this adapter |
| `ollama` | `app/adapters/llm/openai_compatible.py` | `OLLAMA_MODEL` | OpenAI-compatible `response_format={"type":"json_object"}` plus local Pydantic validation | No | No | Not applicable | Text-only in this adapter |

## Operational Recommendations

- Use `openrouter` when you need the mature summarization path: fallback models, OpenRouter model-family routing, provider-order controls, prompt-cache accounting, streaming, and the existing vision-related model wiring.
- Use `openai` when you want direct OpenAI billing, lower proxy surface area, or residency/compliance constraints that forbid OpenRouter as an intermediary.
- Use `anthropic` when you want a direct Anthropic key and direct Messages API behavior. The direct adapter does not yet expose Anthropic prompt-cache controls, so `anthropic` direct mode is not a drop-in replacement for all OpenRouter Anthropic cache workflows.
- Use `ollama` for local or LAN-hosted OpenAI-compatible inference. Expect weaker JSON adherence with many local models; keep model-specific smoke tests in place before relying on it for unattended summaries.
- Keep provider-specific API keys in `.env`; non-secret model names, base URLs, and timeouts can live in `ratatoskr.yaml`.

## Test Coverage

- Direct provider roundtrips: `tests/adapters/llm/test_direct_provider_e2e.py`.
- Provider selection/factory dispatch: `tests/config/test_llm_provider_selection.py`.
- OpenRouter behavior: existing OpenRouter-focused tests under `tests/test_openrouter_*.py`, `tests/unit/llm/`, and related summarization workflow tests.
