# Taskiq Worker Runbook

Use this when background tasks stop running, the Redis broker stalls, scheduled jobs do not enqueue, or jobs reach the dead-letter queue. This runbook complements `docs/runbooks/taskiq-failures.md`, which covers inspecting and replaying individual DLQ rows.

## Symptoms

- Alert `RatatoskrTaskiqDeadLettersHigh`, `RatatoskrSchedulerJobChronicallyFailing`, `RatatoskrVectorReconcilerStopped`, or `RatatoskrScheduledDigestNoDeliveries` fires.
- `worker` is up but no background work completes; digest, GitHub sync, RSS, vector reconcile, import/export, or X bookmark tasks stop progressing.
- Logs contain `taskiq_dead_lettered`, `taskiq_dead_letter_persist_failed`, `scheduler_job_chronic_failure`, `redis_connection_failed`, `github_sync_skipped_lock_held`, or task-specific lock messages.
- Redis is unavailable or overloaded, causing broker, locks, API rate limits, sync sessions, or progress streams to degrade.
- `taskiq_failed_jobs` rows accumulate with `status='dead_letter'`.

## Log Queries

```bash
docker compose -f ops/docker/docker-compose.yml ps worker scheduler redis
docker compose -f ops/docker/docker-compose.yml logs --tail=400 worker scheduler redis | rg 'taskiq|dead_letter|retry|scheduler|redis|lock|broker'
docker exec -i ratatoskr-redis redis-cli ping
docker exec -i ratatoskr-redis redis-cli INFO memory
```

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT id, task_name, attempt_count, status, last_failed_at, left(error_text, 200) AS error FROM taskiq_failed_jobs ORDER BY last_failed_at DESC LIMIT 20;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT task_name, status, count(*) AS rows FROM taskiq_failed_jobs GROUP BY task_name, status ORDER BY rows DESC;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT status, count(*) FROM request_processing_jobs WHERE updated_at > now() - interval '24 hours' GROUP BY status ORDER BY count DESC;"
```

## Prometheus Panels

- Alerts: `RatatoskrTaskiqDeadLettersHigh`, `RatatoskrSchedulerJobChronicallyFailing`, plus subsystem alerts that depend on workers such as `RatatoskrVectorReconcilerStopped` and `RatatoskrScheduledDigestNoDeliveries`.
- Grafana: `Ratatoskr Overview` (`ratatoskr-overview`) panels `Request Rate by Status`, `Error Rate (5m)`, `Database Query Latency`, and `Circuit Breaker State History`.
- Metrics to query directly: `ratatoskr_taskiq_retries_total{outcome="retry|dead_letter|success_after_retry"}` and `ratatoskr_scheduler_job_chronic_failures_total`.

## Mitigation Steps

1. Confirm whether this is worker, scheduler, Redis, or a single task: compare `docker compose ps`, scheduler logs, worker logs, and Redis `PING`.
2. If Redis is down, restart Redis first: `docker compose -f ops/docker/docker-compose.yml restart redis`; then restart `worker` and `scheduler` so broker connections refresh.
3. If the scheduler is down but workers are healthy, restart only `scheduler`; avoid running multiple scheduler instances because each scheduler enqueues cron jobs.
4. If workers are wedged or memory-heavy, restart `worker`; if tasks are idempotent, queued Redis stream entries should be consumed after restart.
5. If one task is holding a Redis lock too long, identify the lock from logs before deleting it; only clear a lock when the owning worker process is confirmed dead.
6. Drain/replay DLQ cautiously: inspect each row, fix the upstream cause, run `python -m app.cli.requeue_failed_task <id> --dry-run`, then replay one row and watch logs/metrics before replaying more.
7. If the queue is overloaded by a noisy subsystem, disable that subsystem's scheduler/config flag temporarily, let critical queues drain, then re-enable after the upstream dependency is stable.

## Escalation

Page the maintainer if Redis data corruption is suspected, multiple unrelated tasks immediately dead-letter after restart, a non-idempotent task may have partially persisted data, or clearing locks/streams manually is required.

## References

- `docs/runbooks/taskiq-failures.md`
- `docs/reference/data-model.md#taskiq_failed_jobs`
- `app/tasks/broker.py`
- `app/tasks/middleware.py`
