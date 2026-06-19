---
name: Quarterly secret rotation drill
about: Track the Ratatoskr secret-rotation drill and sign-off
title: "Quarterly secret rotation drill - YYYY-QN"
labels: ["ops", "security", "drill"]
assignees: ""
---

## Scope

- [ ] `GITHUB_TOKEN_ENCRYPTION_KEY`
- [ ] `JWT_SECRET_KEY`
- [ ] `BOT_TOKEN`
- [ ] `BACKUP_ENCRYPTION_KEY`
- [ ] `MCP_FORWARDING_SECRET`
- [ ] Provider API keys (`OPENROUTER_API_KEY`, direct LLM keys such as `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / optional `OLLAMA_API_KEY`, `ELEVENLABS_API_KEY`, and enabled optional providers)
- [ ] Login peppers (`SECRET_LOGIN_PEPPER`, `CREDENTIALS_LOGIN_PEPPER`)

## Preconditions

- [ ] Read `docs/runbooks/secret-rotation.md`.
- [ ] Confirm current database backup exists and restore path is known.
- [ ] Confirm old and new secret values are stored only in the approved secret manager.
- [ ] Confirm maintenance window and rollback owner.

## Drill Steps

- [ ] Tabletop each secret class and record the exact rotation command or console path.
- [ ] For one low-risk secret class, perform a live rotation or dry-run where supported; automated dry-runs do not replace the annual live human rotation.
- [ ] Verify the service path listed in the runbook.
- [ ] Confirm logs contain no plaintext secret values.
- [ ] Confirm rollback decision and whether rollback was needed.

## Results

- Operator:
- Reviewer:
- Date:
- Deployment:
- Mode: tabletop / dry-run / live
- Result: pass / fail / follow-up required
- Evidence links:

## Follow-Ups

- [ ] Update `docs/runbooks/secret-rotation.md` if any step drifted.
- [ ] File bugs for any missing overlap window, verification command, or rollback gap.
- [ ] Append the completed drill to the runbook sign-off table.
