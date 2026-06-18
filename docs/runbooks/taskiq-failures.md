# Taskiq failures

Taskiq workers use `SimpleRetryMiddleware` for tasks that opt in with `retry_on_error=True` and `max_retries=<n>`. When a task reaches its terminal failed attempt, `TaskiqDeadLetterMiddleware` writes the payload to `taskiq_failed_jobs` and increments `ratatoskr_taskiq_retries_total{outcome="dead_letter"}`.

## Alerts

- `RatatoskrTaskiqDeadLettersHigh`: at least one job reached the dead-letter queue in a 15-minute window.

## Inspect failed jobs

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT id, task_name, attempt_count, status, last_failed_at, left(error_text, 200) AS error FROM taskiq_failed_jobs ORDER BY last_failed_at DESC LIMIT 20;"
```

Inspect a payload before replaying:

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -x -c \
  "SELECT task_name, args_json, kwargs_json, labels_json, error_text, traceback_text FROM taskiq_failed_jobs WHERE id = <id>;"
```

## Requeue one failed task

Run a dry-run first to verify that the task is still registered:

```bash
python -m app.cli.requeue_failed_task <id> --dry-run
```

Then requeue:

```bash
python -m app.cli.requeue_failed_task <id>
```

The CLI removes the internal `_retries` label before replay so the task starts with a fresh retry budget. The row status is changed from `dead_letter` to `requeued` after enqueue succeeds.

## Triage checklist

1. Check whether the failure is transient: upstream API outage, Redis interruption, Qdrant/Postgres restart, network timeout, or provider rate limit.
2. Check whether replay is idempotent for the task. URL processing and sync tasks are designed to tolerate duplicate delivery; avoid replaying unknown ad-hoc tasks until their payload and side effects are understood.
3. Replay a small number of rows and watch `ratatoskr_taskiq_retries_total{outcome="success_after_retry"}` and worker logs before replaying more.
4. If rows immediately dead-letter again, stop replaying and fix the underlying dependency or task bug first.
