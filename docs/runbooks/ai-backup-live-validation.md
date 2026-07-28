# AI Account Backup — Live Validation Runbook

Use this when standing up the AI account backup subsystem for the first time, re-validating it after a sidecar or session change, or diagnosing a run that did not produce output. It covers the full sequence: environment check → session capture → ingest → trigger → inspect → troubleshoot.

## 1. Scope and Terms-of-Service caution

This subsystem mirrors the **operator's own** ChatGPT and Claude web accounts to disk by driving the same internal APIs the web UI calls, from inside an authenticated CloakBrowser session. That technique violates OpenAI's and Anthropic's Terms of Service; Anthropic has demonstrably suspended accounts for session-token reuse (April 2026). The subsystem ships **off by default**, is double-gated (`AI_BACKUP_ENABLED` plus a per-service flag), and is designed as a single-tenant, own-account-only tool. It is not intended for scraping third-party accounts. Claude Enterprise operators should prefer the sanctioned Compliance API path (`AI_BACKUP_CLAUDE_COMPLIANCE_KEY`) over the scrape path wherever it is available.

## 2. Prerequisites

### Infrastructure

- **Provider-isolated re-auth stack** must be running under the `ai-backup-reauth` profile. Confirm all five health gates:

```bash
docker compose -f ops/docker/docker-compose.yml --profile ai-backup-reauth ps \
  ai-backup-display-chatgpt ai-backup-display-claude \
  ai-backup-webauthn-bridge \
  cloakbrowser-reauth-chatgpt cloakbrowser-reauth-claude
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
docker compose -f ops/docker/docker-compose.yml exec -T ratatoskr \
  ls -la /data/ai-backups
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

## 3. Re-authorize interactively (primary)

Open **Account Backups → AI Accounts** in Frost and press **Re-authorize ChatGPT** or **Re-authorize Claude**. An expiry notification opens the same page with `?service=<service>&reauth=1`, which starts the matching flow automatically.

The owner-only page opens a full noVNC remote desktop. Mouse movement, hover,
click, drag, right-click, wheel, normal keyboard shortcuts, touch gestures, and
clipboard work directly in the Chrome window. **Paste clipboard** sends the
local clipboard once; **Copy remote clipboard** writes the most recent remote
clipboard value and immediately clears component memory. Ratatoskr exposes only
the owner-ticketed WSS relay; CDP and VNC remain inside Docker and RFB payloads
are neither interpreted nor logged. For passkey-only accounts, choose Chrome's
phone/tablet option, scan the QR code visible in noVNC, and approve with Face ID
or Touch ID. The phone must be close to the Raspberry Pi: cross-device WebAuthn
uses BLE to verify proximity. A passkey bound only to the workstation still
cannot cross VNC directly.

After the provider accepts the login, the page advances automatically:

```text
waiting_for_user → verifying → resuming_backup → completed
```

`completed` means the fresh encrypted session was accepted by a real targeted provider backup (`ratatoskr.ai_backup.sync_one`), not merely that a cookie appeared. The interactive window expires after 15 minutes; start a new flow if needed. An unexpected network disconnect obtains a fresh one-use viewer ticket and reconnects to the same running browser without losing the page.

Before accepting a deploy, verify isolation and that the lightweight root health
probe answers without requiring a CDP browser launch:

```bash
docker inspect ratatoskr-ai-backup-display-chatgpt ratatoskr-ai-backup-display-claude \
  --format '{{json .NetworkSettings.Ports}}'
docker inspect ratatoskr-cloakbrowser-reauth-chatgpt ratatoskr-cloakbrowser-reauth-claude \
  --format '{{json .NetworkSettings.Ports}}'
docker inspect ratatoskr-ai-backup-webauthn-bridge \
  --format '{{.HostConfig.NetworkMode}} {{json .HostConfig.CapDrop}}'
docker exec ratatoskr-ai-backup-webauthn-bridge \
  /usr/local/bin/ai-backup-webauthn-healthcheck.sh
docker exec ratatoskr-cloakbrowser-reauth-chatgpt python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:9222/').status)"
docker exec ratatoskr-cloakbrowser-reauth-chatgpt python -c \
  "import json,urllib.request; u='http://localhost:9222/json/version?fingerprint=deadbeef0001&timezone=UTC&locale=en-US'; assert json.load(urllib.request.urlopen(u, timeout=20))['webSocketDebuggerUrl']"
docker exec ratatoskr-cloakbrowser-reauth-chatgpt sh -c \
  "test -S /run/ratatoskr-dbus/system_bus_socket; A=\$(ps -eo args); ! printf '%s\n' \"\$A\" | grep '[c]hrome' | grep -F -- '--headless'; printf '%s\n' \"\$A\" | grep '[c]hrome' | grep -F -- '--ozone-platform=x11'"
```

The deploy command runs the same one-time `/json/version` and headed-process
assertion with disposable, non-production seeds for both provider containers
after their lightweight healthchecks, then terminates each smoke Chrome.
All browser/display port maps must be empty (`5900`/`9222` are not published),
and the WebAuthn bridge network mode must be `none`. In each
viewer, confirm that only its provider Chrome is visible, UI language is
English, timezone-sensitive output uses `Asia/Tbilisi`, and disconnect/reconnect
preserves the current page. For hybrid WebAuthn, choose **Use a different phone
or tablet** (provider/Chrome wording may differ), scan the QR code, keep the
phone near the Pi, and approve the passkey. Complete login and observe the
normal terminal flow and a successful targeted backup for both services.

## 4. Manual session blob fallback

The session blob is a Playwright `storage_state` JSON that contains the browser's cookies and localStorage for the service domain. It is the only credential the subsystem stores; no account password is ever persisted.

### Why a bookmarklet cannot work

Both `chatgpt.com` and `claude.ai` set their session cookies with `HttpOnly`, which means JavaScript running in the page context — including a bookmarklet — cannot read `document.cookie`. The blob must be exported from the browser's DevTools storage panel or via the `capture_ai_session.py` helper, which runs Playwright in headful mode and reads `context.storage_state()` after a human completes login.

### Local browser capture

Run the capture script on the operator's workstation (not inside Docker):

```bash
# ChatGPT
python tools/scripts/capture_ai_session.py --service chatgpt --out chatgpt.json

# Claude
python tools/scripts/capture_ai_session.py --service claude --out claude.json
```

The helper builds the storage state in memory and atomically writes the output
with mode `0600`. It refuses a symlink destination. Keep the file on a trusted
local filesystem, upload it over HTTPS, and delete it immediately after ingest.

The script opens a Chromium window. Log in normally (including 2FA). After the dashboard loads, press Enter in the terminal; the script writes the `storage_state` blob to the output file and exits. The file will contain `__Secure-next-auth.session-token` and `cf_clearance` for ChatGPT, or `sessionKey` and `cf_clearance` for Claude.

**`cf_clearance` fingerprint/IP risk.** Cloudflare binds `cf_clearance` to the TLS/JA3 fingerprint and source IP of the browser that solved the challenge. A blob captured from your laptop carries your laptop's fingerprint and IP. If the sidecar runs on a Raspberry Pi with a different public IP, that `cf_clearance` will be re-challenged on the first internal-API call and the run will fail with a `403 cf-mitigated` error. Mode B is the fix for this.

### Capture on the deployment host (preferred when local blobs get 403)

When the sidecar and the operator's workstation have different public IPs, a
locally captured `cf_clearance` is likely to fail. Use the primary Frost noVNC
flow instead: the login runs inside the provider-dedicated CloakBrowser on the
deployment host, so the saved session and subsequent backup share the same
fingerprint and egress IP. No CloakBrowser Manager or public CDP endpoint is
needed. Manual blob ingest remains only a recovery path for environments where
the interactive viewer cannot be used.

## 5. Ingest the session blob

The blob never transits Telegram (the bot surfaces only status commands). There are two ways to store it.

### 5a. CLI ingest (recommended for single-tenant self-host — no JWT)

Run inside the container; it validates the provider session cookie, encrypts the blob into `user_browser_sessions` for the owner (first `ALLOWED_USER_IDS`), and marks authorization `unverified` until the next provider check — no Mobile-API JWT needed. Unlike the REST/Frost fallback, the CLI does not enqueue a Taskiq run, so trigger one explicitly in section 6:

```bash
docker compose -f ops/docker/docker-compose.yml exec -T ratatoskr sh -c \
  'set -eu; umask 077; tmp=$(mktemp /tmp/ai-backup-session.XXXXXX); trap "rm -f \"$tmp\"" EXIT HUP INT TERM; cat >"$tmp"; python -m app.cli.ai_backup --ingest "$tmp" --service chatgpt' \
  < chatgpt.json
```

It prints the cookie names found (never values). Exit code `0` on success, `2` on an unreadable/invalid blob or empty `ALLOWED_USER_IDS`.

### 5b. REST ingest (when posting from a remote host)

Post the blob over HTTPS with a valid Mobile-API JWT for the owner user:

```bash
# Replace <token> with a valid JWT and <service> with chatgpt or claude.
curl -s -X POST https://<host>/v1/ai-backups/<service>/session \
    -H "Authorization: Bearer <token>" \
    -H "Content-Type: application/json" \
    -d @chatgpt.json
```

- **204** — blob accepted and encrypted into `user_browser_sessions`; an immediate targeted verification backup was queued.
- **400** — malformed blob (missing required cookie or localStorage key for the service).
- **401** — expired or invalid JWT.

To remove Ratatoskr's stored authorization for a provider, use the same owner
JWT. This is idempotent and does not sign the account out at the provider:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE \
    https://<host>/v1/ai-backups/<service>/session \
    -H "Authorization: Bearer <token>"
```

Expect **204**, then verify the provider reports `authorization_status=missing`.

Verify the row was persisted:

```bash
docker compose -f ops/docker/docker-compose.yml exec -T postgres \
  psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT id, domain, created_at, updated_at FROM user_browser_sessions WHERE domain IN ('chatgpt.com','claude.ai') ORDER BY updated_at DESC LIMIT 5;"
```

## 6. Trigger a run

### Immediate (synchronous, full log output)

```bash
docker compose -f ops/docker/docker-compose.yml exec -T ratatoskr \
  python -m app.cli.ai_backup --service chatgpt
```

Logs stream to stdout. Use `--service claude` for the Claude path, or omit `--service` to run all enabled services in sequence.

### Scheduled (next cron fire)

The Taskiq scheduler enqueues `ratatoskr.ai_backup.sync` at the `AI_BACKUP_SYNC_CRON` cadence (default `0 5 * * *` UTC) when `AI_BACKUP_ENABLED=true`. To force immediate dispatch via the broker:

```bash
docker compose -f ops/docker/docker-compose.yml exec -T ratatoskr \
  python -m taskiq kiq ratatoskr.ai_backup.sync
```

Confirm the lock is not already held before dispatching:

```bash
docker compose -f ops/docker/docker-compose.yml exec -T redis \
  redis-cli EXISTS task_lock:ai_backup_sync
```

A result of `1` means a run is already in progress (TTL 1800 s). Wait for it to finish or, if the owning worker is dead, delete the key after confirming the worker PID is gone.

## 7. Inspect output

### On-disk tree

```
AI_BACKUP_DATA_PATH/<service>/<YYYY-MM-DD>/
  conversations/<conversation_id>.json
  projects/<project_id>/project.json
  files/<file_id>__<filename>
  artifacts/<conversation_id>/<artifact_id>.<ext>   # Claude only
  manifest.json
```

Check the run directory:

```bash
docker compose -f ops/docker/docker-compose.yml exec -T ratatoskr \
  ls -lh /data/ai-backups/chatgpt/$(date +%Y-%m-%d)/
docker compose -f ops/docker/docker-compose.yml exec -T ratatoskr \
  cat /data/ai-backups/chatgpt/$(date +%Y-%m-%d)/manifest.json | python -m json.tool
```

`manifest.json` contains `counts`, `requests_made`, `skipped_incremental`, `incremental`, `correlation_id`, and the run timestamp.

Before the run, create an owner-only expected-inventory file outside the
repository. Values are the stable provider IDs recorded for the disposable
validation corpus; never attach this file to a ticket or commit it:

```json
{
  "service": "chatgpt",
  "run_date": "2026-07-17",
  "conversations": ["<conversation-id>"],
  "projects": ["<project-id>"],
  "files": ["<file-id>"],
  "artifacts": []
}
```

Verify the completed run offline. The verifier checks manifest schema v2,
identity and counts, exact expected inventory, every payload SHA-256 by
no-follow read-back, owner-only file/directory modes, and rejects symlinks or
unhashed files. Its JSON output contains aggregate counts and the manifest hash, but no
provider IDs, correlation ID, filenames, or content:

```bash
chmod 600 /secure/expected-chatgpt.json
docker compose -f ops/docker/docker-compose.yml exec -T ratatoskr sh -c \
  'set -eu; umask 077; tmp=$(mktemp /tmp/ai-backup-inventory.XXXXXX); trap "rm -f \"$tmp\"" EXIT HUP INT TERM; cat >"$tmp"; python -m app.cli.verify_ai_backup --run-dir /data/ai-backups/chatgpt/$(date +%Y-%m-%d) --expected-inventory "$tmp"' \
  < /secure/expected-chatgpt.json
```

Exit status `0` records `offline_integrity_passed`; any mismatch or unsafe path
exits `1`. The output deliberately leaves `provider_compatibility` and
`project_knowledge` as `unverified`. Run it after both the full and incremental
sweeps, preserving each snapshot before a same-day run overwrites its manifest,
and use the exact inventory expected in that run directory. This does not prove
that a partial manifest came from a successful run: retain the lifecycle
`status=ok` row and `ai_backup_run_complete` log marker alongside this evidence.
It also does not replace the live provider-contract and project-knowledge checks
in the external acceptance gate.

### Lifecycle row

```bash
docker compose -f ops/docker/docker-compose.yml exec -T postgres \
  psql -U ratatoskr_app -d ratatoskr -x -c \
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
| `403 cf-mitigated` in logs or HTML Cloudflare interstitial in conversation JSON | `cf_clearance` captured from a different IP/fingerprint than the sidecar | Re-authorize through the Frost noVNC viewer so the clearance cookie is created by the deployment's dedicated CloakBrowser fingerprint and egress IP |
| `authorization_status=expired` in `ai_account_backups` | Session cookie expired or rotated; the service halted to avoid hammering a login wall | Re-run Mode A or Mode B to capture a fresh blob, then re-ingest via `POST /v1/ai-backups/<service>/session`; it becomes `unverified` and changes to `valid` only after a successful provider run |
| ChatGPT Projects return 404 (`gizmos/snorlax`) | The `snorlax` internal codename has changed; this endpoint is soft-fail by design | Check OpenAI web traffic for the updated path; update `chatgpt_client.py`; the run continues with conversations only until the path is fixed |
| HTTP 429 / rate-limit errors during a run | Request cadence too aggressive, or a large account whose full sweep exceeds the provider's per-window quota (ChatGPT is far stricter than Claude) | Increase `AI_BACKUP_REQUEST_DELAY_MS` (default 1500 ms) and optionally lower `AI_BACKUP_MAX_REQUESTS_PER_RUN`. A 429 no longer discards progress: conversations already written stay on disk, a partial manifest is recorded, and the **next run resumes** — it skips conversations already saved for that run date and fetches only what is missing. So a large account converges across successive runs (manual re-run or the daily cron after the backoff window), each making far fewer requests, until one run completes with `status=ok`. |
| `counts_json` is `{}` or all zeros after a successful run | Field-path mismatch in the internal-API response — the `TODO(live-validation)` markers in `chatgpt_client.py` and `claude_client.py` flag paths that have not yet been verified against live accounts | Inspect the raw conversation JSON saved to disk and compare field names with what the client extracts; update the client and file a follow-up under `docs/tasks/issues/ai-account-backup-cloakbrowser.md` |
| `ai_backup_no_session` in logs, run exits immediately | No session blob has been ingested for this service | Run Mode A or B and POST the blob via `POST /v1/ai-backups/<service>/session` |
| `ai_backup_sync_skipped_lock_held` and nothing runs | A previous run is still active (or the worker died while holding the lock) | Confirm whether a worker process is alive; if not, `docker compose -f ops/docker/docker-compose.yml exec -T redis redis-cli DEL task_lock:ai_backup_sync` then re-trigger |
| `ai_backup_sync_no_owner` warning | `ALLOWED_USER_IDS` is empty | Set `ALLOWED_USER_IDS` to the operator's Telegram user ID |

## 8. Known limitations

- **`TODO(live-validation)` markers.** The field paths used by both `ChatGptBackupClient` (`app/adapters/ai_backup/chatgpt_client.py`) and `ClaudeBackupClient` (`app/adapters/ai_backup/claude_client.py`) are reverse-engineered from web-UI traffic and have **not yet been validated against live accounts**. Empty or misshapen output after a successful run is the primary symptom of a path drift.
- **ChatGPT Deep Research structured citations.** Only the final report text is captured. The machine-readable `url_citation` objects and the reasoning trace are not exposed by the `/backend-api` surface; they require the paid developer Responses API.
- **ChatGPT Custom GPT system prompts.** No confirmed internal endpoint has been identified that exposes these; they are not currently captured.
- **Claude project knowledge.** The current client stores Project metadata only. Text knowledge and binary attachments are not downloaded; both remain blocked on live validation of the project-doc/project-file contracts.
- **Claude Compliance API path.** `AI_BACKUP_CLAUDE_COMPLIANCE_KEY` is reserved but the Compliance client is not implemented. Setting the key makes the client factory fail closed instead of running the browser scrape. Claude Enterprise operators should leave the subsystem off (`AI_BACKUP_CLAUDE_ENABLED=false`) until the sanctioned client is implemented.

## References

- `docs/explanation/ai-account-backup.md`
- `docs/tasks/issues/ai-account-backup-cloakbrowser.md`
- `app/config/ai_backup.py`
- `app/adapters/ai_backup/`
- `app/tasks/ai_backup_sync.py`
- `docs/runbooks/scraper-chain.md` (CloakBrowser sidecar ops)
- `docs/runbooks/secret-rotation.md` (`GITHUB_TOKEN_ENCRYPTION_KEY` rotation)
