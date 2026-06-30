# Common Debugging Scenarios

## 1. "Firecrawl returns empty content"

Check:

```bash
# View raw response
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr "
  SELECT raw_response_json
  FROM crawl_results
  WHERE request_id = '<correlation_id>';
" | python -m json.tool

# Check if PDF parser needed
grep -r "parsers.*pdf" app/adapters/content/
```

## 2. "LLM returns invalid JSON"

Check `app/core/json_utils.py`:

- Uses `json_repair` library to fix malformed output
- Falls back through multiple parsing strategies
- Logs repair attempts with correlation ID

## 3. "Rate limit errors"

```bash
# Count recent API calls
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT count(*) AS calls_last_hour
     FROM llm_calls
    WHERE created_at > now() - interval '1 hour';"
```

## 4. "High API costs"

```bash
# Analyze token usage and costs
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr <<'EOF'
SELECT
  model,
  count(*) AS calls,
  avg(tokens_prompt) AS avg_prompt,
  avg(tokens_completion) AS avg_completion,
  sum(cost_usd) AS total_cost
FROM llm_calls
WHERE status = 'ok'
GROUP BY model
ORDER BY total_cost DESC;
EOF
```
