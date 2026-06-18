---
name: Quarterly disaster-recovery restore drill
about: Track a Ratatoskr backup restore drill and sign-off
title: "Quarterly DR restore drill - YYYY-QN"
labels: ["ops", "drill", "disaster-recovery"]
assignees: ""
---

## Scope

- [ ] Read `docs/runbooks/disaster-recovery.md`.
- [ ] Identify the restore point and backup artifact timestamp.
- [ ] Confirm the artifact metadata JSON includes `timestamp`, `size_bytes`, and `sha256`.
- [ ] Confirm the selected backup is within the 24h RPO target or record the gap.
- [ ] Confirm the target RTO for this drill is 1 hour or record the exception.

## Preconditions

- [ ] Production writers are not touched; this drill runs against a disposable database or host.
- [ ] `BACKUP_ENCRYPTION_KEY` is available from the approved secret store if the artifact is encrypted.
- [ ] Operator and reviewer are assigned.
- [ ] Rollback/cleanup owner is assigned.

## Restore Steps

- [ ] Run `tools/scripts/restore_smoke.sh tests/fixtures/restore_smoke.dump` against disposable Postgres 16.
- [ ] Restore the selected PostgreSQL backup into a non-production database or host.
- [ ] Run Alembic migrations for the current image.
- [ ] Choose Qdrant restore or rebuild and record collection counts.
- [ ] Choose Redis restore or reset and record the reason.
- [ ] Run the verification checklist from `docs/runbooks/disaster-recovery.md`.
- [ ] Run a healthcheck and one owner-visible smoke path.

## Results

- Operator:
- Reviewer:
- Date:
- Deployment or host:
- Mode: tabletop / disposable restore / live incident
- Backup artifact:
- Metadata SHA256:
- Started at:
- Postgres restored at:
- App verified at:
- Measured RTO:
- Measured RPO:
- Result: pass / fail / follow-up required
- Evidence links:

## Verification Notes

- Postgres row counts:
- Latest summary timestamp:
- Alembic revision:
- Qdrant collection counts or rebuild summary:
- Redis restore/reset result:
- Healthcheck result:
- Telegram/API smoke result:

## Follow-Ups

- [ ] Update `docs/runbooks/disaster-recovery.md` if any command drifted.
- [ ] File bugs for any restore, verification, alerting, or documentation gaps.
- [ ] Append the completed drill to the runbook sign-off table.
