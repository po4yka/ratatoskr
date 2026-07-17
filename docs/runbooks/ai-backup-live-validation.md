# AI Account Backup — Live Validation Runbook

Use this when standing up the AI account backup subsystem for the first time, re-validating it after a sidecar or session change, or diagnosing a run that did not produce output. It covers the full sequence: environment check → session capture → ingest → trigger → inspect → troubleshoot.

## 1. Scope and Terms-of-Service caution

This subsystem mirrors the **operator's own** ChatGPT and Claude web accounts to disk by driving the same internal APIs the web UI calls, from inside an authenticated CloakBrowser session. That technique violates OpenAI's and Anthropic's Terms of Service; Anthropic has demonstrably suspended accounts for session-token reuse (April 2026). The subsystem ships **off by default**, is double-gated (`AI_BACKUP_ENABLED` plus a per-service flag), and is designed as a single-tenant, own-account-only tool. It is not intended for scraping third-party accounts. Claude Enterprise operators should prefer the sanctioned Compliance API path (`AI_BACKUP_CLAUDE_COMPLIANCE_KEY`) over the scrape path wherever it is available.

## 2. Prerequisites

### Infrastructure

- **CloakBrowser sidecar** must be running under the `with-scrapers` Docker Compose profile and reachable at `SCRAPER_CLOAKBROWSER_URL` (default `http://cloakbrowser:9222`). Confirm with:

```bash
docker compose -f ops/docker/docker-compose.yml --profile with-scrapers ps cloakbrowser
docker compose -f ops/docker/docker-compose.yml --profile with-scrapers logs --tail=50 cloakbrowser
```

- **Taskiq worker** running (the sync job is dispatched via the broker):

```bash
docker compose -f ops/docker/docker-compose.yml ps worker scheduler
```

- **Postgres** and **Redis** reachable (standard stack-up, same check as `taskiq-worker.md`).
- **`GITHUB_TOKEN_ENCRYPTION_KEY`** must be set; the subsystem reuses this Fernet key to encrypt and decrypt session blobs in `user_browser_sessions`. There is no separate key surface.

### Environment

Set all of the following before starting the scheduler or triggering a manual run:

```bash
AI_BACKUP_ENABLED=true
AI_BACKUP_CHATGPT_ENABLED=true    # and/or
AI_BACKUP_CLAUDE_ENABLED=true
AI_BACKUP_DATA_PATH=/data/ai-backups   # must be an absolute path; bind-mount writable from the host
```

Optional but recommended for first validation:

```bash
AI_BACKUP_INCREMENTAL=false           # force a full download on first run
AI_BACKUP_NOTIFY_ON=always            # surface success/failure via Telegram
AI_BACKUP_NOTIFY_CHAT_ID=<your_chat_id>
AI_BACKUP_HC_PING_URL=https://hc-ping.com/<uuid>   # dead-man switch
```

`AI_BACKUP_DATA_PATH` must be bind-mounted writable into the container. Verify:

```bash
docker exec -it ratatoskr ls -la /data/ai-backups
```

**Owner ID:** the task keys every backup row on the **first** Telegram user ID in `ALLOWED_USER_IDS`. Ensure that `ALLOWED_USER_IDS` is non-empty and that the first ID is the operator whose session will be submitted.

### External acceptance gate (not reproducible with fixtures)

The internal provider contracts and project-knowledge coverage cannot be
certified from mocks. Treat live-account validation as blocked until an
operator supplies all of the following:

1. Explicit approval to access the operator's own ChatGPT and/or Claude account
   through undocumented web APIs, acknowledging the Terms-of-Service and
   account-suspension risk. Never use a third-party account.
2. A disposable or otherwise risk-accepted account with a recorded expected
   inventory. For ChatGPT include ordinary and archived conversations, one
   Project with project instructions/knowledge, and at least one attachment. For
   Claude include ordinary conversations, one Project with text knowledge, and
   one Artifact. Record expected stable IDs and counts without copying content or
   credentials into tickets.
3. A freshly captured owner session containing the required service cookie and
   `cf_clearance`, captured through the same CloakBrowser fingerprint and public
   egress IP that will run the validation (Mode B when those differ).
4. A non-production validation deployment with outbound access to only the
   configured provider allowlist, both relevant feature flags enabled, a writable
   empty backup root, enough disk headroom, and request/byte caps sized for the
   recorded corpus. Keep Claude Compliance mode disabled unless a dedicated
   sanctioned client is being validated; the current placeholder must fail closed.
5. Authorization to retain redacted validation evidence: run correlation ID,
   endpoint status codes, expected-versus-observed IDs/counts, manifest hashes,
   file modes, and a restore/read-back check. Session blobs, cookie values,
   access tokens, account identifiers, and conversation content must not enter
   logs, screenshots, or repository artifacts.

Completion requires observing both a full successful sweep and a second
incremental sweep for each enabled provider, verifying project knowledge and
attachments against the recorded inventory, then revoking the stored session.
Until that evidence exists, report provider/project-knowledge compatibility as
**unverified external blocker**, not as passed based on fixture tests.

## 3. Produce the session blob

The session blob is a Playwright `storage_state` JSON that contains the browser's cookies and localStorage for the service domain. It is the only credential the subsystem stores; no account password is ever persisted.

### Why a bookmarklet cannot work

Both `chatgpt.com` and `claude.ai` set their session cookies with `HttpOnly`, which means JavaScript running in the page context — including a bookmarklet — cannot read `document.cookie`. The blob must be exported from the browser's DevTools storage panel or via the `capture_ai_session.py` helper, which runs Playwright in headful mode and reads `context.storage_state()` after a human completes login.

### Mode A — local browser (primary)

Run the capture script on the operator's workstation (not inside Docker):

```bash
# ChatGPT
python tools/scripts/capture_ai_session.py --service chatgpt --out chatgpt.json

# Claude
python tools/scripts/capture_ai_session.py --service claude --out claude.json
```

The script opens a Chromium window. Log in normally (including 2FA). After the dashboard loads, press Enter in the terminal; the script writes the `storage_state` blob to the output file and exits. The file will contain `__Secure-next-auth.session-token` and `cf_clearance` for ChatGPT, or `sessionKey` and `cf_clearance` for Claude.

**`cf_clearance` fingerprint/IP risk.** Cloudflare binds `cf_clearance` to the TLS/JA3 fingerprint and source IP of the browser that solved the challenge. A blob captured from your laptop carries your laptop's fingerprint and IP. If the sidecar runs on a Raspberry Pi with a different public IP, that `cf_clearance` will be re-challenged on the first internal-API call and the run will fail with a `403 cf-mitigated` error. Mode B is the fix for this.

### Mode B — capture inside the sidecar (preferred when Mode A blobs get 403)

When the sidecar and the operator's workstation share a public IP (common for a home-lab Pi behind NAT) Mode A works fine. When they differ, capture the session from *inside* the sidecar so the `cf_clearance` fingerprint and IP match the profile that will make the backup requests:

```bash
python tools/scripts/capture_ai_session.py --service chatgpt \
    --cdp ws://cloakbrowser:9222 \
    --out chatgpt.json
```

This requires a display; the sidecar runs headless by default. [CloakBrowser-Manager](https://github.com/CloakHQ/CloakBrowser-Manager) (early-alpha) provides a noVNC viewer that exposes the sidecar's display over HTTP so the operator can log in interactively from a browser tab. Consult its README for the `docker compose` overlay that enables noVNC. Once the manager is up, open the noVNC URL in a browser, log into the AI service manually inside that session, then pass the CDP WebSocket URL to the capture script via `--cdp`.

## 4. Ingest the session blob

The blob never transits Telegram (the bot surfaces only status commands). There are two ways to store it.

### 4a. CLI ingest (recommended for single-tenant self-host — no JWT)

Run inside the container; it validates the provider session cookie, encrypts the blob into `user_browser_sessions` for the owner (first `ALLOWED_USER_IDS`), and marks authorization `unverified` until the next provider check — no Mobile-API JWT needed:

```bash
docker cp chatgpt.json ratatoskr:/tmp/chatgpt.json
docker exec -it ratatoskr python -m app.cli.ai_backup --ingest /tmp/chatgpt.json --service chatgpt
docker exec -it ratatoskr rm -f /tmp/chatgpt.json   # the blob holds live cookies
```

It prints the cookie names found (never values). Exit code `0` on success, `2` on an unreadable/invalid blob or empty `ALLOWED_USER_IDS`.

### 4b. REST ingest (when posting from a remote host)

Post the blob over HTTPS with a valid Mobile-API JWT for the owner user:

```bash
# Replace <token> with a valid JWT and <service> with chatgpt or claude.
curl -s -X POST https://<host>/v1/ai-backups/<service>/session \
    -H "Authorization: Bearer <token>" \
    -H "Content-Type: application/json" \
    -d @chatgpt.json
```

- **204** — blob accepted and encrypted into `user_browser_sessions`.
- **400** — malformed blob (missing required cookie or localStorage key for the service).
- **401** — expired or invalid JWT.

Verify the row was persisted:

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT id, domain, created_at, updated_at FROM user_browser_sessions WHERE domain IN ('chatgpt.com','claude.ai') ORDER BY updated_at DESC LIMIT 5;"
```

## 5. Trigger a run

### Immediate (synchronous, full log output)

```bash
docker exec -it ratatoskr python -m app.cli.ai_backup --service chatgpt
```

Logs stream to stdout. Use `--service claude` for the Claude path, or omit `--service` to run all enabled services in sequence.

### Scheduled (next cron fire)

The Taskiq scheduler enqueues `ratatoskr.ai_backup.sync` at the `AI_BACKUP_SYNC_CRON` cadence (default `0 5 * * *` UTC) when `AI_BACKUP_ENABLED=true`. To force immediate dispatch via the broker:

```bash
docker exec -it ratatoskr python -m taskiq kiq ratatoskr.ai_backup.sync
```

Confirm the lock is not already held before dispatching:

```bash
docker exec -i ratatoskr-redis redis-cli EXISTS task_lock:ai_backup_sync
```

A result of `1` means a run is already in progress (TTL 1800 s). Wait for it to finish or, if the owning worker is dead, delete the key after confirming the worker PID is gone.

## 6. Inspect output

### On-disk tree

```
AI_BACKUP_DATA_PATH/<service>/<YYYY-MM-DD>/
  conversations/<conversation_id>.json
  projects/<project_id>/project.json
  projects/<project_id>/knowledge/<file>
  files/<file_id>__<filename>
  artifacts/<conversation_id>/<artifact_id>.<ext>   # Claude only
  manifest.json
```

Check the run directory:

```bash
docker exec -it ratatoskr ls -lh /data/ai-backups/chatgpt/$(date +%Y-%m-%d)/
docker exec -it ratatoskr cat /data/ai-backups/chatgpt/$(date +%Y-%m-%d)/manifest.json | python -m json.tool
```

`manifest.json` contains `counts`, `requests_made`, `skipped_incremental`, `incremental`, `correlation_id`, and the run timestamp.

### Lifecycle row

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -x -c \
  "SELECT service, status, counts_json, last_backup_path, last_backed_up_at, consecutive_failures, last_error
   FROM ai_account_backups
   ORDER BY updated_at DESC;"
```

A healthy completed run shows `status=ok`, a non-null `last_backup_path`, and `counts_json` with non-zero values for `conversations` (and `projects`, `files`, `artifacts` if applicable). `consecutive_failures` should be 0.

### Log markers for a healthy run

```bash
docker compose -f ops/docker/docker-compose.yml logs --tail=200 worker | \
    rg 'ai_backup_run_complete|ai_backup_auth_expired|ai_backup_service_run_failed|ai_backup_backoff_active'
```

A successful run emits `ai_backup_run_complete` with a `counts` field. Absence of this marker after the run time indicates the task short-circuited (no session, backoff active, or lock held).

## 7. Troubleshooting

| Symptom | Likely cause | Resolution |
|---|---|---|
| `403 cf-mitigated` in logs or HTML Cloudflare interstitial in conversation JSON | `cf_clearance` captured from a different IP/fingerprint than the sidecar | Recapture the session using Mode B (`--cdp ws://cloakbrowser:9222`) so the clearance cookie matches the sidecar's fingerprint and IP |
| `authorization_status=expired` in `ai_account_backups` | Session cookie expired or rotated; the service halted to avoid hammering a login wall | Re-run Mode A or Mode B to capture a fresh blob, then re-ingest via `POST /v1/ai-backups/<service>/session`; it becomes `unverified` and changes to `valid` only after a successful provider run |
| ChatGPT Projects return 404 (`gizmos/snorlax`) | The `snorlax` internal codename has changed; this endpoint is soft-fail by design | Check OpenAI web traffic for the updated path; update `chatgpt_client.py`; the run continues with conversations only until the path is fixed |
| HTTP 429 / rate-limit errors during a run | Request cadence too aggressive, or a large account whose full sweep exceeds the provider's per-window quota (ChatGPT is far stricter than Claude) | Increase `AI_BACKUP_REQUEST_DELAY_MS` (default 1500 ms) and optionally lower `AI_BACKUP_MAX_REQUESTS_PER_RUN`. A 429 no longer discards progress: conversations already written stay on disk, a partial manifest is recorded, and the **next run resumes** — it skips conversations already saved for that run date and fetches only what is missing. So a large account converges across successive runs (manual re-run or the daily cron after the backoff window), each making far fewer requests, until one run completes with `status=ok`. |
| `counts_json` is `{}` or all zeros after a successful run | Field-path mismatch in the internal-API response — the `TODO(live-validation)` markers in `chatgpt_client.py` and `claude_client.py` flag paths that have not yet been verified against live accounts | Inspect the raw conversation JSON saved to disk and compare field names with what the client extracts; update the client and file a follow-up under `docs/tasks/issues/ai-account-backup-cloakbrowser.md` |
| `ai_backup_no_session` in logs, run exits immediately | No session blob has been ingested for this service | Run Mode A or B and POST the blob via `POST /v1/ai-backups/<service>/session` |
| `ai_backup_sync_skipped_lock_held` and nothing runs | A previous run is still active (or the worker died while holding the lock) | Confirm whether a worker process is alive; if not, `docker exec -it ratatoskr-redis redis-cli DEL task_lock:ai_backup_sync` then re-trigger |
| `ai_backup_sync_no_owner` warning | `ALLOWED_USER_IDS` is empty | Set `ALLOWED_USER_IDS` to the operator's Telegram user ID |

## 8. Known limitations

- **`TODO(live-validation)` markers.** The field paths used by both `ChatGptBackupClient` (`app/adapters/ai_backup/chatgpt_client.py`) and `ClaudeBackupClient` (`app/adapters/ai_backup/claude_client.py`) are reverse-engineered from web-UI traffic and have **not yet been validated against live accounts**. Empty or misshapen output after a successful run is the primary symptom of a path drift.
- **ChatGPT Deep Research structured citations.** Only the final report text is captured. The machine-readable `url_citation` objects and the reasoning trace are not exposed by the `/backend-api` surface; they require the paid developer Responses API.
- **ChatGPT Custom GPT system prompts.** No confirmed internal endpoint has been identified that exposes these; they are not currently captured.
- **Claude project binary files.** Project-knowledge text files are captured; binary attachments via the project-files path are not yet implemented.
- **Claude Compliance API path.** `AI_BACKUP_CLAUDE_COMPLIANCE_KEY` is reserved but the Compliance client is not implemented. Setting the key makes the client factory fail closed instead of running the browser scrape. Claude Enterprise operators should leave the subsystem off (`AI_BACKUP_CLAUDE_ENABLED=false`) until the sanctioned client is implemented.

## References

- `docs/explanation/ai-account-backup.md`
- `docs/tasks/issues/ai-account-backup-cloakbrowser.md`
- `app/config/ai_backup.py`
- `app/adapters/ai_backup/`
- `app/tasks/ai_backup_sync.py`
- `docs/runbooks/scraper-chain.md` (CloakBrowser sidecar ops)
- `docs/runbooks/secret-rotation.md` (`GITHUB_TOKEN_ENCRYPTION_KEY` rotation)
