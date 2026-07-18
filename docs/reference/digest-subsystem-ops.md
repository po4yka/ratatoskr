# Channel Digest Subsystem — Ops Reference

Operational guide for the channel-digest scheduler: how it runs, what can go wrong, and how to diagnose problems.

## Architecture

```
Taskiq Scheduler process
  └─ _AppConfigScheduleSource → emits "ratatoskr.digest.run" cron tasks
        │
        ▼
Taskiq Worker process  (one or more replicas)
  └─ run_channel_digest()
        └─ _channel_digest_body(cfg)
              ├─ Redis lock  (ratatoskr:digest:scheduled:lock, 10-min TTL)
              ├─ UserbotClient.start()        ← Telethon userbot session
              ├─ DigestService.generate_digest() per user
              │     ├─ ChannelReader   → fetch posts via userbot
              │     ├─ DigestAnalyzer  → configured LLM analysis
              │     ├─ DigestFormatter → Telegram message chunks
              │     └─ DigestStore.async_create_delivery() → DigestDelivery row
              └─ UserbotClient.stop()
```

## Configuration

| Env var | Default | Description |
|---|---|---|
| `DIGEST_ENABLED` | `false` | Must be `true` to enable the subsystem |
| `DIGEST_TIMES` | `10:00,19:00` | Comma-separated HH:MM delivery times |
| `DIGEST_TIMEZONE` | `UTC` | Timezone for delivery times (IANA name) |
| `DIGEST_MAX_POSTS` | `20` | Max posts per digest run |
| `DIGEST_HOURS_LOOKBACK` | `24` | How far back to fetch channel posts |
| `DIGEST_MIN_RELEVANCE` | `0.3` | Min LLM relevance score to include a post |
| `DIGEST_CONCURRENCY` | `3` | Parallel LLM analysis calls |
| `DIGEST_SESSION_NAME` | `channel_digest_userbot` | Telethon session file name |

Full reference: `docs/reference/environment-variables.md`.

## Starting the Scheduler

The scheduler and workers run as separate processes (see `ops/docker/`):

```bash
# Scheduler — emits tasks on cron schedule
taskiq scheduler app.tasks.scheduler:scheduler

# Worker — consumes and executes tasks
python -m app.cli.taskiq_worker app.tasks.digest app.tasks.rss
```

In Docker Compose, these are the `scheduler` and `worker` services.

## Distributed Lock

Each scheduled run acquires a Redis key `ratatoskr:digest:scheduled:lock` (10-minute TTL) before executing.  This prevents multiple worker replicas from double-delivering when the task is picked up concurrently.

**Redis unavailable** → lock is skipped and the digest proceeds anyway (graceful degrade).  Each replica will run independently — acceptable for low-replica deployments, but monitor `DigestDelivery` counts if you run many workers.

**Lock stuck** (worker crashed mid-run) → the TTL (10 min) ensures the lock expires automatically.  No manual intervention is needed unless `DIGEST_TIMES` are more frequent than 10 minutes.

## Userbot Session

The Telethon userbot session is stored at the path derived from `DIGEST_SESSION_NAME`.  The session file is **reused** across runs — `start()` attaches to the existing session rather than creating a new one.

**First-time setup**: the session must be initialised interactively via the `/init_session` bot command (OTP/2FA flow through the Telegram Mini App) before the scheduled job can run.

## Monitoring

Structured log events emitted by the digest task:

| Event | Level | Meaning |
|---|---|---|
| `scheduled_digest_starting` | INFO | Run began; `cid` is the correlation ID |
| `scheduled_digest_users` | INFO | Number of users to process |
| `scheduled_digest_user_complete` | INFO | Per-user success; `posts` and `errors` counts |
| `scheduled_digest_user_failed` | ERROR | Per-user exception (skipped, others continue) |
| `scheduled_digest_failed` | ERROR | Fatal error before any user was processed |
| `digest_lock_held_skipping` | WARNING | Another instance holds the lock; this run skipped |
| `digest_lock_redis_unavailable` | WARNING | Redis unreachable; lock bypassed, run proceeds |

All events carry `cid` (correlation ID) of the form `digest_YYYYMMDD_HHMMSS`. Per-user events additionally carry `uid`.

Query recent deliveries:

```sql
SELECT user_id, delivered_at, post_count, channel_count, digest_type, correlation_id
FROM digest_deliveries
ORDER BY delivered_at DESC
LIMIT 20;
```

## Failure Modes

### Redis unavailable

**Symptom**: `digest_lock_redis_unavailable` warning in logs.

**Behaviour**: Digest runs without distributed locking.  If multiple workers are active, they may all deliver — resulting in duplicate messages for the same window.

**Resolution**: Restore Redis connectivity.  The subsystem self-recovers on the next run once Redis is reachable.

---

### Telethon auth expired (`AuthKeyUnregisteredError`)

**Symptom**: `scheduled_digest_failed` error with `AuthKeyUnregisteredError` in the message.

**Behaviour**: `userbot.start()` raises; the exception is caught, logged with the correlation ID, and the run exits cleanly.  `userbot.stop()` is always called (no session leak).

**Resolution**:

1. Check `DIGEST_SESSION_NAME` — ensure the session file exists at the expected path inside the container (`/data/<session_name>.session`).
2. Re-run the OTP/2FA flow via `/init_session` in the Telegram bot to re-authorise the session.
3. After re-auth, the next scheduled run will succeed automatically.

---

### No posts delivered (empty digest)

**Symptom**: `scheduled_digest_user_complete` logs show `posts=0`; users receive "Нет новых постов" messages.

**Possible causes**:

- All posts already delivered in a previous run (`DigestDelivery.posts_json` tracks delivered IDs for 30 days).
- Channel posts are shorter than `DIGEST_MIN_POST_LENGTH` characters.
- All posts scored below `DIGEST_MIN_RELEVANCE` by the LLM.
- `DIGEST_HOURS_LOOKBACK` window too narrow for the channel's posting cadence.

---

### Double delivery (duplicate messages)

**Symptom**: Users receive the same digest twice.

**Likely cause**: Two worker replicas picked up the same task concurrently while Redis was unavailable (lock bypassed).

**Resolution**: Ensure Redis is healthy.  The `DigestDelivery.posts_json` column records delivered post IDs; subsequent runs filter them out, so the duplication is bounded to a single window.
