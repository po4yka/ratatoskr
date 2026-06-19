# GitHub Sync Runbook

Use this when starred-repository sync stops, GitHub API rate limits pause imports, stored PAT/OAuth credentials are revoked, or repository analysis backlog grows.

## Symptoms

- Alert `RatatoskrSchedulerJobChronicallyFailing`, `RatatoskrTaskiqDeadLettersHigh`, `RatatoskrHighErrorRate`, or `RatatoskrLLMRetryExhaustionHigh` fires during or after the `ratatoskr.github.sync_stars` job.
- GitHub status shows `needs_reauth`, repository lists stop updating, or github.com URL ingestion returns `GitHub integration required`.
- Logs contain `github_sync_rate_limit`, `github_sync_auth_error`, `github_sync_user_error`, `github_sync_skipped_lock_held`, `needs_reauth_dm_skipped`, or `ratatoskr.github.sync_stars`.
- `repositories.pending_analysis=true` grows because `GITHUB_SYNC_LLM_DAILY_BUDGET` was reached or LLM analysis failed.
- `user_github_integrations.last_sync_cursor` contains a GitHub sync state with `backoff_until` after a rate-limit response.

## Log Queries

```bash
docker compose -f ops/docker/docker-compose.yml logs --tail=400 worker scheduler ratatoskr mobile-api | rg 'github_sync|github|ratatoskr.github.sync_stars|needs_reauth|rate_limit|pending_analysis'
docker compose -f ops/docker/docker-compose.yml logs --since=2h worker | rg 'github_sync_(starting|completed|rate_limit|auth_error|user_error)'
```

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT user_id, github_login, status, last_synced_at, updated_at, left(last_sync_cursor, 240) AS sync_state FROM user_github_integrations ORDER BY updated_at DESC;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT user_id, count(*) AS repos, count(*) FILTER (WHERE pending_analysis) AS pending_analysis, max(last_synced_at) AS latest_repo_sync FROM repositories GROUP BY user_id ORDER BY pending_analysis DESC;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT id, task_name, attempt_count, status, last_failed_at, left(error_text, 200) AS error FROM taskiq_failed_jobs WHERE task_name = 'ratatoskr.github.sync_stars' ORDER BY last_failed_at DESC LIMIT 10;"
```

## Prometheus Panels

- Alerts: no GitHub-specific Prometheus alert exists yet; use `RatatoskrSchedulerJobChronicallyFailing`, `RatatoskrTaskiqDeadLettersHigh`, `RatatoskrHighErrorRate`, and `RatatoskrLLMRetryExhaustionHigh` as the current alert coverage.
- Grafana: `Ratatoskr Overview` (`ratatoskr-overview`) panels `Request Rate by Status`, `Error Rate (5m)`, `OpenRouter Latency by Model`, `OpenRouter Cost (per hour)`, and `Circuit Breaker State History`.
- Metrics to query directly: repository sync metrics from `app/observability/metrics_repositories.py` such as `ratatoskr_github_sync_runs_total`, `ratatoskr_github_sync_repos_imported_total`, `ratatoskr_github_sync_repos_updated_total`, `ratatoskr_github_sync_repos_unstarred_total`, `ratatoskr_github_sync_llm_calls_total`, and `ratatoskr_github_pending_analysis_backlog`.

## Mitigation Steps

1. Confirm the job is registered and scheduled: `docker compose -f ops/docker/docker-compose.yml ps worker scheduler`; then inspect scheduler logs for `ratatoskr.github.sync_stars`.
2. If `github_sync_rate_limit` appears, read `reset_epoch` from logs or `last_sync_cursor`, do not retry in a loop, and wait until reset unless the GitHub token can be replaced with a higher-quota token.
3. If status is `needs_reauth`, ask the owner/user to reconnect with `POST /v1/auth/github/pat` or the web Preferences GitHub Integration panel; do not edit encrypted token columns by hand.
4. If `GITHUB_TOKEN_ENCRYPTION_KEY` changed or decrypt errors appear, follow `docs/runbooks/secret-rotation.md`; if the previous key is lost, affected users must reconnect.
5. If `pending_analysis` is high because the daily budget was reached, either wait for the next day or temporarily raise `GITHUB_SYNC_LLM_DAILY_BUDGET` after checking LLM cost alerts.
6. Run a dry-run before manual replay: `python -m app.cli.sync_github_stars --user-id <id> --dry-run`; then run `python -m app.cli.sync_github_stars --user-id <id>` once.
7. If the Taskiq job dead-lettered, inspect `taskiq_failed_jobs`, fix the upstream GitHub/LLM/config issue, then replay using `python -m app.cli.requeue_failed_task <id>`.

## Escalation

Page the maintainer if GitHub sync marks multiple active integrations `needs_reauth` unexpectedly, the encryption key cannot decrypt stored tokens, rate-limit backoff persists beyond GitHub's reset time, or repository imports mutate/deleted unexpected user data.

## References

- `docs/guides/configure-source-ingestors.md#github-repositories`
- `docs/reference/troubleshooting.md#github-integration-issues`
- `docs/reference/cli-commands.md#github-stars-sync`
- `app/tasks/github_sync.py`
