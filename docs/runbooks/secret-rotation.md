# Secret Rotation Runbook

Use this runbook for planned rotation, suspected exposure, and quarterly drills. Keep real secret values out of tickets, logs, screenshots, and commit messages; record only timestamps, owners, affected deployment names, and verification results.

## Scope

This runbook covers `GITHUB_TOKEN_ENCRYPTION_KEY`, `JWT_SECRET_KEY`, `BOT_TOKEN`, `BACKUP_ENCRYPTION_KEY`, `MCP_FORWARDING_SECRET`, LLM/provider keys (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, optional `OLLAMA_API_KEY`, `ELEVENLABS_API_KEY`, and enabled scraper/search/provider keys), `SECRET_LOGIN_PEPPER`, and `CREDENTIALS_LOGIN_PEPPER`. Rotate provider keys at the provider first, then update Ratatoskr configuration; rotate local signing/encryption keys by adding an overlap window where the code supports one.

## Cadence

Run a tabletop drill quarterly and a live rotation drill at least annually. Open `.github/ISSUE_TEMPLATE/rotate-secrets-quarterly.md`, assign one operator and one reviewer, and close the issue only after verification commands and rollback notes are filled in. An automated repo drill can validate documentation, parsing, overlap tests, and dry-run tooling, but it does not replace a human live rotation of a real low-risk secret.

## Common Rules

- Generate replacement secrets on the target host or a trusted admin machine with `openssl rand -hex 32`, `python tools/scripts/generate_github_encryption_key.py`, or the provider console as appropriate.
- Update the deployment secret store or `.env`, redeploy, verify, then remove overlap/old-key settings after the documented backfill or token expiry window.
- Never paste secret values into logs or GitHub issues. Use fingerprints such as `sha256:<first-12-hex>` only when you need to prove which secret version is active.
- Before rotating anything that encrypts stored data, take a database backup and keep the old key available until verification passes.

## GitHub Token Encryption Key

Secret: `GITHUB_TOKEN_ENCRYPTION_KEY`. Previous-key window: `GITHUB_TOKEN_PREVIOUS_KEYS`. Blast radius if mishandled: stored GitHub PAT/OAuth tokens and Webwright cookies cannot be decrypted; GitHub sync, repository ingestion, authenticated git mirrors, and browser sessions degrade or require reconnect.

Rotation steps:

1. Generate the new Fernet key: `python tools/scripts/generate_github_encryption_key.py`.
2. Move the current `GITHUB_TOKEN_ENCRYPTION_KEY` value into `GITHUB_TOKEN_PREVIOUS_KEYS`; set the generated key as `GITHUB_TOKEN_ENCRYPTION_KEY`.
3. Redeploy and verify that existing integrations still decrypt: `python -m app.cli.rotate_github_tokens --dry-run`.
4. Backfill ciphertext with the new primary key: `python -m app.cli.rotate_github_tokens`.
5. Redeploy with `GITHUB_TOKEN_PREVIOUS_KEYS` removed.
6. Verify: `python -m app.cli.rotate_github_tokens --dry-run` should report `failed: 0`; GitHub sync and authenticated mirror jobs should complete without decryption errors.

Rollback: restore the previous key as primary and remove the new key from config if decrypt failures appear before backfill completes. If the old key is lost, users must reconnect integrations and browser sessions.

## JWT Signing Secret

Secret: `JWT_SECRET_KEY`. Previous-key window: `JWT_SECRET_PREVIOUS_KEYS`. Blast radius if mishandled: existing API, web, mobile, browser-extension, and MCP JWT sessions fail until users reauthenticate.

Rotation steps:

1. Generate the new signing secret: `openssl rand -hex 32`.
2. Move the current `JWT_SECRET_KEY` into `JWT_SECRET_PREVIOUS_KEYS`; set the generated value as `JWT_SECRET_KEY`.
3. Redeploy. New tokens are signed with the new key; existing tokens signed by previous keys remain accepted until they expire.
4. Wait at least the longest refresh-token TTL in use, or force all users to reauthenticate if this is an incident rotation.
5. Remove `JWT_SECRET_PREVIOUS_KEYS` and redeploy.
6. Verify: log in through each enabled auth mode, refresh a session, and confirm old sessions either continue during the overlap window or fail after the overlap is removed.

Rollback: restore the old `JWT_SECRET_KEY` and remove the new key if token verification fails immediately after deployment. Do not keep `JWT_SECRET_PREVIOUS_KEYS` populated longer than the planned window.

## Telegram Bot Token

Secret: `BOT_TOKEN`. No overlap window. Blast radius if mishandled: the Telegram bot cannot receive updates or send replies.

Rotation steps:

1. In BotFather, revoke/regenerate the token for the bot.
2. Update `BOT_TOKEN` in the deployment secret store or `.env`.
3. Restart the bot container/process.
4. Verify: send `/start` from an allowed owner and confirm the bot replies; check startup logs for Telegram authentication errors.

Rollback: BotFather token regeneration invalidates the old token. If the new token was entered incorrectly, copy it again from BotFather and restart.

## Backup Encryption Key

Secret: `BACKUP_ENCRYPTION_KEY`. No automatic multi-key backfill exists for backup archives. Blast radius if mishandled: encrypted backup archives cannot be restored without the key that encrypted them.

Rotation steps:

1. Before changing the key, restore-test the newest encrypted backup with the current key.
2. Generate a new Fernet key using the same command documented for backup setup: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
3. Update `BACKUP_ENCRYPTION_KEY` and redeploy.
4. Create a fresh backup and run a dry-run restore against it.
5. Keep the old backup key in the external password manager until every backup encrypted with it has expired under retention policy.

Rollback: restore the previous `BACKUP_ENCRYPTION_KEY` if newly-created backups cannot be verified or restored.

## MCP Forwarding Secret

Secret: `MCP_FORWARDING_SECRET`. Header names: `MCP_FORWARDED_SECRET_HEADER` and `MCP_FORWARDED_ACCESS_TOKEN_HEADER`. Blast radius if mishandled: hosted MCP requests behind a trusted gateway are rejected or, if left stale after gateway exposure, a compromised gateway secret remains usable.

Rotation steps:

1. Generate a new shared secret: `openssl rand -hex 32`.
2. Configure the trusted gateway to send the new secret and deploy Ratatoskr with the new `MCP_FORWARDING_SECRET` in the same maintenance window.
3. Verify: call the hosted MCP endpoint through the gateway and confirm forwarded JWT auth succeeds; direct requests without the forwarding secret must fail.

Rollback: restore the previous gateway and Ratatoskr values together.

## Provider API Keys

Secrets: `OPENROUTER_API_KEY`, direct LLM keys such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, optional `OLLAMA_API_KEY`, `ELEVENLABS_API_KEY`, optional scraper/provider keys such as `FIRECRAWL_API_KEY`, `WHISPER_API_KEY`, `GEMINI_API_KEY`, and `QDRANT_API_KEY`. Blast radius if mishandled: provider calls fail, but stored local data remains readable.

Rotation steps:

1. Create the replacement key in the provider console with the same or narrower permissions.
2. Update the deployment secret store or `.env`, then redeploy.
3. Verify a low-cost health path for that provider, such as one summary request for OpenRouter or one TTS request for ElevenLabs.
4. Revoke the old provider key in the provider console after verification passes.

Rollback: re-enable or restore the old provider key only if it was not rotated due to suspected compromise.

## Login Peppers

Secrets: `SECRET_LOGIN_PEPPER` and `CREDENTIALS_LOGIN_PEPPER`. Blast radius if mishandled: existing client secrets or user credentials may fail verification until reissued/reset. These peppers are intentionally independent from `JWT_SECRET_KEY`.

Rotation steps:

1. Prefer forced re-issue/reset over silent pepper replacement. Create a communication window for any clients/users affected by secret-login or credentials-login.
2. Generate the new pepper with `openssl rand -hex 32`.
3. For `SECRET_LOGIN_PEPPER`, rotate client secrets through `/v1/auth/secret-keys` so plaintext is shown once to the owner/client operator.
4. For `CREDENTIALS_LOGIN_PEPPER`, force password reset/re-enrollment for affected credential users.
5. Update the pepper and redeploy only after the re-issue/reset plan is ready.
6. Verify: a newly-issued client secret or reset password works; an intentionally old credential no longer works after the cutover.

Rollback: restore the previous pepper if the cutover was accidental and old credentials must remain valid. Treat rollback after suspected compromise as unsafe.

## Drill Sign-Off

Append a row here after every drill or live rotation. Use the GitHub issue for detailed evidence and keep this table as the durable index.

| Date | Scope | Mode | Operator | Reviewer | Result | Evidence |
|---|---|---|---|---|---|---|
| 2026-06-18 | Runbook created | Documentation | Codex | Pending human reviewer | Pending first operator drill | `.github/ISSUE_TEMPLATE/rotate-secrets-quarterly.md` |
| 2026-06-19 | All named secret classes; GitHub token re-encryption and JWT overlap verified by focused tests; provider-key inventory drift patched | Automated tabletop + dry-run validation | Codex | Human reviewer pending | Pass for repo-verifiable drill; live human rotation still due for the annual requirement | `pytest tests/cli/test_rotate_github_tokens.py tests/security/test_secret_crypto.py tests/security/test_token_crypto.py tests/api/auth/test_jwt_tokens.py -q`; `.github/ISSUE_TEMPLATE/rotate-secrets-quarterly.md` |
