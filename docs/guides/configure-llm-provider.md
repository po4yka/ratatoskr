# Configure LLM Provider

Ratatoskr uses `LLM_PROVIDER` or `runtime.llm_provider` to choose the LLM adapter used by the summarization workflow. The supported values are `openrouter`, `openai`, `anthropic`, and `ollama`.

Use `.env` for provider API keys and `ratatoskr.yaml` for non-secret model and tuning choices. YAML secret keys are ignored by the config loader, so keep `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, and `ANTHROPIC_API_KEY` outside committed YAML.

## OpenRouter Default

OpenRouter is the default and the broadest production path. Use it when you want fallback models, provider routing, OpenRouter structured-output capability checks, streaming, and the existing prompt-cache knobs.

```env
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
```

```yaml
runtime:
  llm_provider: openrouter
openrouter:
  model: deepseek/deepseek-v4-flash
  fallback_models:
    - qwen/qwen3.6-flash
    - minimax/minimax-m2
  flash_model: qwen/qwen3.6-flash
  flash_fallback_models:
    - qwen/qwen3.6-plus-04-02
  long_context_model: minimax/minimax-m2
```

## Direct OpenAI

Use direct OpenAI when you want OpenAI billing, a smaller intermediary surface, or an OpenAI-compatible private gateway.

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

```yaml
runtime:
  llm_provider: openai
openai:
  model: gpt-4o-mini
  base_url: https://api.openai.com/v1
  timeout_sec: 60
  max_retries: 3
```

## Direct Anthropic

Use direct Anthropic when you want Anthropic billing or direct Messages API behavior. The adapter validates JSON returned by the model, but it does not currently expose Anthropic prompt-cache controls.

```env
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

```yaml
runtime:
  llm_provider: anthropic
anthropic:
  model: claude-sonnet-4-5
  base_url: https://api.anthropic.com/v1
  version: "2023-06-01"
  max_tokens: 4096
  timeout_sec: 60
  max_retries: 3
```

## Ollama

Use Ollama for local OpenAI-compatible inference. The API key is optional. Set `OLLAMA_BASE_URL` when Ollama is not on `localhost:11434`.

```env
LLM_PROVIDER=ollama
```

```yaml
runtime:
  llm_provider: ollama
ollama:
  model: llama3.2
  base_url: http://ollama:11434/v1
  timeout_sec: 120
  max_retries: 1
```

## Validation

Run the provider selection and mocked provider roundtrip tests before deploying a provider switch:

```bash
source .venv/bin/activate
pytest tests/config/test_llm_provider_selection.py tests/adapters/llm/test_direct_provider_e2e.py -q
```

For a live smoke test, run the CLI summary command against a small public URL after setting the provider-specific key and model:

```bash
python -m app.cli.summary --url https://example.com
```

## Tradeoffs

| Need | Prefer |
| --- | --- |
| Mature fallback ladder and model routing | `openrouter` |
| Direct OpenAI billing or private OpenAI-compatible gateway | `openai` |
| Direct Anthropic account control | `anthropic` |
| Local/offline-ish experimentation | `ollama` |
| Lowest operational surprise for unattended summaries | `openrouter` |
