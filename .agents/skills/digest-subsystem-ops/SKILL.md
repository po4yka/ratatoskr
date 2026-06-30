---
name: digest-subsystem-ops
description: Operate and debug the Telegram channel digest subsystem (separate Telethon userbot session, Mini App OTP/2FA session init). Trigger keywords -- digest, channels, userbot, /init_session, /digest, /subscribe, channel_post, digest_delivery, Mini App, OTP, 2FA.
version: 1.0.0
allowed-tools: Bash, Read, Grep
---

# Digest Subsystem Operations

The channel digest subsystem produces scheduled digests of subscribed Telegram channels. It runs a SEPARATE Telethon userbot session (because bots can't read channel post content), initialized via a Telegram Mini App OTP/2FA flow brokered through the Mobile API.

## Architecture

```
User -> Bot (`/init_session`) -> Mobile API (Mini App) -> OTP/2FA -> Userbot session file
User -> Bot (`/subscribe @channel`) -> ChannelSubscription row
Scheduler -> Userbot fetches channel posts -> ChannelPost rows
Scheduler -> ChannelPostAnalysis (LLM) -> DigestDelivery -> Bot sends digest
```

The bot identity and the userbot identity are different Telegram accounts. The userbot account belongs to the project owner (you).

## Enabling

```bash
# .env
DIGEST_ENABLED=true
API_BASE_URL=http://localhost:8000   # Mobile API base URL the Mini App calls back to
```

The Mobile API (FastAPI) must be reachable from the Mini App for session init.

## User-Facing Commands

| Command | Effect |
| ------- | ------ |
| `/init_session` | Owner-only. Bot replies with a Mini App link; user opens it inside Telegram, enters their phone, then the OTP, then 2FA password if set. Persists a Telethon session file. |
| `/digest` | Force-deliver a digest for the configured period |
| `/channels` | List subscribed channels |
| `/subscribe @channel` | Subscribe to a channel by username |
| `/unsubscribe @channel` | Remove a subscription |

## DB Tables

| Table | Purpose |
| ----- | ------- |
| `channels` | Channel metadata (id, username, title, category) |
| `channel_subscriptions` | User <-> channel mapping with per-subscription preferences |
| `channel_posts` | Raw post text fetched by the userbot |
| `channel_post_analysis` | LLM analysis (relevance, summary, topic) per post |
| `digest_deliveries` | What was sent when, to whom |
| `user_digest_preferences` | Per-user digest schedule and content settings |

All defined in `app/db/models/digest.py`.

## Common Queries

### Has the userbot session been initialized?

The Telethon session lives on disk, not in Postgres -- the bot reads/writes a `.session` file in the data directory configured for the userbot.

```bash
# Check the on-disk session file (path may vary by config; default lives in data/)
ls -la data/*.session 2>&1

# If running in the container, inspect inside it:
docker exec -i ratatoskr ls -la /data/*.session 2>&1
```

If no file exists, `/init_session` has not been completed successfully.

### Recent post ingestion

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT c.username, count(cp.*) AS posts, max(cp.posted_at) AS latest
     FROM channels c LEFT JOIN channel_posts cp ON cp.channel_id = c.id
    WHERE cp.created_at > now() - interval '24 hours'
    GROUP BY c.username ORDER BY posts DESC;"
```

### Pending analyses

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT count(*) FROM channel_posts cp
    LEFT JOIN channel_post_analysis a ON a.channel_post_id = cp.id
    WHERE a.id IS NULL AND cp.created_at > now() - interval '7 days';"
```

### Digest delivery history for a user

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT user_id, period_start, period_end, posts_included, sent_at, status
     FROM digest_deliveries
    WHERE user_id = <telegram_user_id>
    ORDER BY sent_at DESC LIMIT 10;"
```

## Session-Init Flow Failure Modes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Mini App link doesn't open | `API_BASE_URL` not reachable from Telegram CDN | Ensure the Mobile API is publicly reachable (Cloudflare tunnel, etc.) |
| OTP never arrives | Wrong phone format / Telegram-side throttle | Wait 24h before retrying; use international format `+...` |
| 2FA loop | Cloud password mismatch | The Mini App asks for the cloud password explicitly; type the Telegram 2FA password, not the OTP |
| Session works briefly then dies | Telegram flagged the userbot for unusual behavior | Re-init; if recurring, check that the userbot isn't being polled too aggressively |

## Cron / Scheduler

The digest scheduler runs inside the bot process via APScheduler with Redis distributed locks (so multiple bot instances don't deliver duplicates). Schedule is configured per-user in `user_digest_preferences`, not via a global cron.

## Key Files

- **Userbot client**: `app/adapters/digest/userbot_client.py` (separate process from the bot's Telethon client)
- **Channel reader**: `app/adapters/digest/channel_reader.py`
- **Digest service**: `app/adapters/digest/digest_service.py`
- **Analyzer (LLM)**: `app/adapters/digest/analyzer.py`
- **Session validator**: `app/adapters/digest/session_validator.py`
- **Session init state**: `app/adapters/telegram/session_init_state.py` (in-memory state machine for the bot-mediated init flow)
- **Models**: `app/db/models/digest.py`
- **Commands**: `app/adapters/telegram/command_handlers/init_session_handler.py`, `digest_handler.py` (and related handlers in the same directory)
- **Mobile API session init**: `app/api/routers/auth/` (grep for `session` to find the exact route)
- **Mini App UI**: `web/src/` (if frontend hosts the Mini App)
- **Ops doc**: `docs/reference/digest-subsystem-ops.md`

## Important Notes

- The userbot account is a REAL Telegram account belonging to the operator -- treat its session file as a secret.
- Session files (`*.session`) are gitignored; never commit them.
- Telegram MTProto rate limits apply to the userbot identity, not the bot identity -- aggressive fetching gets the userbot banned, not the bot.
- The digest subsystem is owner-only by default; `DIGEST_ENABLED=false` short-circuits all related commands.
- For new channels, fetching starts only after `/subscribe` -- the userbot does not backfill historical posts by default.
