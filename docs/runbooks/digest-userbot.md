# Digest Userbot Runbook

Use this when scheduled channel digests stop delivering, `/init_session` fails, the Telethon userbot session expires, or subscribed channels disappear from digest output. The digest userbot is a real Telegram account session; treat its `.session` file as a secret.

## Symptoms

- Alert `RatatoskrDigestDeliveryFailureRateHigh`, `RatatoskrScheduledDigestNoDeliveries`, or `RatatoskrDigestUserbotReconnectsHigh` fires from `ops/monitoring/alerting_rules.yml`.
- Users see no scheduled digest despite active subscriptions, or `/digest` returns an error with an `Error ID`.
- Logs contain `digest_delivery`, `digest_analysis_failed`, `digest_send_chunk_failed`, `digest_userbot_reconnect`, `digest_lock_redis_unavailable`, or Telethon auth/session errors.
- `/init_session` Mini App flow loops on OTP/2FA, fails to reach the API callback, or no `.session` file exists in the configured data directory.
- A channel was removed, renamed, made private, or the userbot was kicked, so post ingestion drops for one channel while the rest continue.

## Log Queries

```bash
docker compose -f ops/docker/docker-compose.yml logs --tail=300 ratatoskr worker scheduler | rg 'digest|userbot|init_session|channel_post|Telethon|session'
docker compose -f ops/docker/docker-compose.yml logs --since=2h worker | rg 'digest_delivery|digest_analysis_failed|digest_send_chunk_failed|ratatoskr.digest'
docker exec -i ratatoskr ls -la /data/*.session 2>&1
```

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT user_id, period_start, period_end, posts_included, status, sent_at FROM digest_deliveries ORDER BY sent_at DESC LIMIT 20;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT c.username, count(cp.*) AS posts, max(cp.posted_at) AS latest FROM channels c LEFT JOIN channel_posts cp ON cp.channel_id = c.id WHERE cp.created_at > now() - interval '24 hours' GROUP BY c.username ORDER BY posts DESC;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT count(*) AS pending_analysis FROM channel_posts cp LEFT JOIN channel_post_analysis a ON a.channel_post_id = cp.id WHERE a.id IS NULL AND cp.created_at > now() - interval '7 days';"
```

## Prometheus Panels

- Alerts: `RatatoskrDigestDeliveryFailureRateHigh`, `RatatoskrScheduledDigestNoDeliveries`, `RatatoskrDigestUserbotReconnectsHigh`.
- Grafana: `Ratatoskr Overview` (`ratatoskr-overview`) panels `Request Rate by Type`, `Request Rate by Status`, `Error Rate (5m)`, and `Circuit Breaker State History`.
- Metrics to query directly when no dashboard panel exists yet: `ratatoskr_digest_deliveries_total`, `ratatoskr_digest_active_subscription_users`, `ratatoskr_digest_userbot_reconnects_total`.

## Mitigation Steps

1. Confirm this is digest-specific: check that normal Telegram commands and URL summaries still work, then inspect `worker` and `scheduler` logs for `ratatoskr.digest.run`.
2. If Redis lock warnings appear, verify Redis first with `docker exec -i ratatoskr-redis redis-cli ping`; restart only Redis-dependent services if the broker was down: `docker compose -f ops/docker/docker-compose.yml restart redis worker scheduler`.
3. If the userbot session file is missing or Telethon reports expired/invalid auth, run `/init_session` from the owner account, complete the Mini App phone/OTP/2FA flow, then verify the session file appears under `/data`.
4. If OTP does not arrive or Telegram throttles the flow, stop retrying; wait for the Telegram cooldown and keep scheduled digest disabled for that channel/user until the userbot session is valid.
5. If one channel stopped ingesting, check whether the userbot account can open it in Telegram; if kicked or private, remove the subscription with `/unsubscribe @channel` or rejoin with the userbot account before resubscribing.
6. If pending analyses are high but posts are being fetched, treat it as an LLM issue and follow `docs/runbooks/llm-cascade.md`; do not repeatedly trigger `/digest` until the backlog is understood.
7. After mitigation, run one manual digest with `/digest` or `POST /v1/digest/trigger` and watch `digest_deliveries.status` for `sent` or `empty`.

## Escalation

Page the maintainer if session reinitialization fails twice, Telegram reports account restrictions, digest delivery is down for more than one scheduled period with active subscriptions, Redis lock failures recur after restart, or the runbook requires deleting channel/digest rows manually.

## References

- `docs/reference/digest-subsystem-ops.md`
- `.codex/skills/digest-subsystem-ops/SKILL.md`
- `app/tasks/digest.py`
- `app/adapters/digest/`
