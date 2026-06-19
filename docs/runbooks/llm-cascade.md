# LLM Cascade Runbook

Use this when summaries fail inside the LLM step, retry budgets exhaust, one model is rate-limited, OpenRouter is degraded, or direct provider mode starts returning invalid JSON.

## Symptoms

- Alert `RatatoskrLLMRetryExhaustionHigh`, `RatatoskrOpenRouterHighLatency`, `RatatoskrHighOpenRouterSpending`, `RatatoskrVeryHighDailySpending`, `RatatoskrCircuitBreakerOpen`, or `RatatoskrCircuitBreakerStuckOpen` fires.
- User-visible error includes an `Error ID` after extraction succeeded.
- Logs contain `openrouter_exhausted`, `openrouter_error`, `openrouter_fallback`, `llm_retry_budget`, `CallBudgetExceeded`, `repair budget exhausted`, or provider 429/5xx messages.
- `llm_calls` shows repeated `status='error'`, many fallback attempts, or repair-loop rows for the same request.
- Summaries are created but fail validation repeatedly, suggesting prompt/schema/provider drift rather than upstream outage.

## Log Queries

```bash
docker compose -f ops/docker/docker-compose.yml logs --tail=400 ratatoskr worker | rg 'openrouter|llm|retry_budget|CallBudgetExceeded|repair budget|json_schema|rate_limit|429|provider'
docker compose -f ops/docker/docker-compose.yml logs --since=30m ratatoskr worker | rg '<correlation_id>|openrouter|llm'
```

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT model, status, count(*) AS calls, round(avg(latency_ms)) AS avg_ms FROM llm_calls WHERE created_at > now() - interval '1 hour' GROUP BY model, status ORDER BY calls DESC;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT attempt_index, attempt_trigger, model, status, tokens_prompt, tokens_completion, cost_usd, left(error_text, 160) AS error FROM llm_calls WHERE request_id = (SELECT id FROM requests WHERE correlation_id = '<correlation_id>') ORDER BY attempt_index;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT r.correlation_id, r.input_url, count(*) AS attempts, max(l.created_at) AS latest FROM llm_calls l JOIN requests r ON r.id = l.request_id WHERE l.created_at > now() - interval '1 hour' GROUP BY r.correlation_id, r.input_url HAVING count(*) >= 4 ORDER BY latest DESC LIMIT 20;"
```

## Prometheus Panels

- Alerts: `RatatoskrLLMRetryExhaustionHigh`, `RatatoskrOpenRouterHighLatency`, `RatatoskrHighOpenRouterSpending`, `RatatoskrVeryHighDailySpending`, `RatatoskrCircuitBreakerOpen`, `RatatoskrCircuitBreakerStuckOpen`.
- Grafana: `Ratatoskr Overview` (`ratatoskr-overview`) panels `OpenRouter Circuit Breaker`, `OpenRouter Latency by Model`, `Token Usage by Model (per hour)`, `OpenRouter Cost (per hour)`, `Circuit Breaker State History`, and `Error Rate (5m)`.
- Metrics to query directly: `ratatoskr_llm_call_attempts_total`, `ratatoskr_llm_call_retry_exhaustion_total`, `ratatoskr_llm_call_latency_seconds`, `ratatoskr_openrouter_cost_usd_total`.

## Mitigation Steps

1. Separate extraction failures from LLM failures by checking that `crawl_results` or platform extraction succeeded for the same request before focusing on LLM.
2. If one model is rate-limited or slow, move it later in `openrouter.fallback_models`, lower `OPENROUTER_PROVIDER_ORDER` priority for the affected upstream, or temporarily select a known-good primary model in `ratatoskr.yaml`.
3. If OpenRouter is down but direct provider credentials are available, switch `runtime.llm_provider` to `openai`, `anthropic`, or `ollama` only after confirming the direct model passes `pytest tests/config/test_llm_provider_selection.py tests/adapters/llm/test_direct_provider_e2e.py -q` locally and the provider-specific key/model are set.
4. If the retry budget is exhausted because content is too large, reduce the input path first: verify article extraction quality, lower optional enrichment, or use the long-context model only for long content.
5. If validation/repair loops exhaust, inspect `llm_calls.error_text` and the prompt payload for schema mismatch; do not raise `MAX_REPAIR_ATTEMPTS` during an incident unless the error is clearly transient.
6. If spending alerts fire, disable expensive last-resort flows such as Webwright/ScrapeGraphAI first, then reduce LLM daily budgets or switch to cheaper fallback models.
7. After changes, run one CLI summary against a small public URL and one previously failing URL, then confirm `llm_calls.status` ends in success and retry exhaustion stops increasing.

## Escalation

Page the maintainer if all configured providers fail, retry exhaustion stays above the alert threshold for more than 30 minutes, schema validation breaks for multiple unrelated inputs after a prompt/schema change, or cost alerts indicate runaway spend that cannot be stopped by disabling optional LLM-heavy features.

## References

- `docs/reference/llm-retry-telemetry.md`
- `docs/reference/llm-providers.md`
- `docs/guides/configure-llm-provider.md`
- `.codex/skills/langgraph-summarize-loop/SKILL.md`
