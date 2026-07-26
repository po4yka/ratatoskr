---
name: digest-subsystem-ops
description: Operate and debug the Taskiq-scheduled Telegram channel digest subsystem and its separate Telethon userbot session. Trigger keywords -- digest, channels, userbot, /init_session, /digest, /subscribe, channel_posts, digest_deliveries, Mini App, OTP, 2FA.
version: 2.0.0
allowed-tools: Bash, Read, Grep
---

# Digest Subsystem Operations

The digest subsystem uses a separate Telethon userbot identity to read subscribed channels. Session initialization is brokered by the bot, Mobile API, and Telegram Mini App OTP/2FA flow.

## Runtime architecture

```text
Taskiq scheduler process
  -> app.tasks.scheduler emits ratatoskr.digest.run at DIGEST_TIMES
  -> RedisStreamBroker queues the task
  -> Taskiq worker runs app.tasks.digest.run_channel_digest
  -> Redis lock prevents duplicate scheduled delivery
  -> UserbotClient reads channels
  -> DigestService analyzes and sends each user's digest
```

The scheduler only enqueues tasks. The worker executes them. The bot's Telethon client and the digest userbot are different Telegram identities.

## Configuration

```env
DIGEST_ENABLED=true
DIGEST_TIMES=10:00,19:00
DIGEST_TIMEZONE=UTC
API_BASE_URL=https://api.example.com
```

`DIGEST_TIMES` and `DIGEST_TIMEZONE` define the global Taskiq cron schedule. Per-user rows in `user_digest_preferences` control lookback, limits, relevance, and delivery preferences; they do not dynamically create scheduler jobs.

The Mobile API must be publicly reachable from Telegram for `/init_session` Mini App callbacks.

## User-facing commands

| Command | Effect |
| --- | --- |
| `/init_session` | Initialize or repair the owner userbot session via Mini App OTP/2FA |
| `/digest` | Generate an on-demand digest |
| `/channels` | List subscriptions |
| `/subscribe @channel` | Subscribe to a channel |
| `/unsubscribe @channel` | Remove a subscription |

## Database checks

Recent post ingestion:

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT c.username, count(cp.*) AS posts, max(cp.date) AS latest
     FROM channels c LEFT JOIN channel_posts cp ON cp.channel_id = c.id
    WHERE cp.created_at > now() - interval '24 hours'
    GROUP BY c.username ORDER BY posts DESC;"
```

Pending analyses:

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT count(*) FROM channel_posts cp
    LEFT JOIN channel_post_analyses a ON a.channel_post_id = cp.id
    WHERE a.id IS NULL AND cp.created_at > now() - interval '7 days';"
```

Delivery history:

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT user_id, delivered_at, post_count, channel_count, digest_type, correlation_id
     FROM digest_deliveries
    WHERE user_id = <telegram_user_id>
    ORDER BY delivered_at DESC LIMIT 10;"
```

## Triage

- Missing/expired session: complete `/init_session` again and verify `/data/<session_name>.session` in the worker container.
- No scheduled run: inspect the `scheduler` service, `DIGEST_ENABLED`, `DIGEST_TIMES`, timezone, and broker connectivity.
- Task queued but no delivery: inspect the `worker` service and `scheduled_digest_*` structured events.
- Duplicate delivery: inspect Redis availability and `ratatoskr:digest:scheduled:lock` warnings.
- Empty digest: check subscriptions, lookback, minimum post length, relevance threshold, and prior `posts_json` delivery history.

## Key files

- Scheduler source: `app/tasks/scheduler.py`
- Task and distributed lock: `app/tasks/digest.py`
- Userbot and digest logic: `app/adapters/digest/`
- Models: `app/db/models/digest.py`
- Commands: `app/adapters/telegram/command_handlers/init_session_handler.py`, `digest_handler.py`
- Mobile API session routes: `app/api/routers/auth/`
- Mini App source: external `ratatoskr-web` repository
- Full ops reference: `docs/reference/digest-subsystem-ops.md`

Treat Telethon session files as secrets. Never commit or print their contents.
