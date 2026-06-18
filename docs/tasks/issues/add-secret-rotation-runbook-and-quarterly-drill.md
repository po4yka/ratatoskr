---
title: Execute first secret-rotation drill
status: backlog
area: ops
priority: medium
owner: unassigned
blocks: []
blocked_by: []
created: 2026-05-17
updated: 2026-06-18
---

- [ ] #task Execute first secret-rotation drill #repo/ratatoskr #area/ops #status/backlog 🔼

## Objective

Run the first human secret-rotation drill using `docs/runbooks/secret-rotation.md` and `.github/ISSUE_TEMPLATE/rotate-secrets-quarterly.md`. The runbook, JWT overlap support, docs links, and issue template now exist; this task remains only for the real operator drill and sign-off.

## Context

- Runbook: `docs/runbooks/secret-rotation.md`.
- Drill issue template: `.github/ISSUE_TEMPLATE/rotate-secrets-quarterly.md`.
- JWT overlap variable: `JWT_SECRET_PREVIOUS_KEYS`.
- Existing CLI: `app/cli/rotate_github_tokens.py`.

## Scope

- Open the quarterly drill issue template.
- Perform a tabletop drill across all named secret classes.
- Perform one low-risk live or dry-run rotation where feasible.
- Append the completed drill result to the sign-off table in `docs/runbooks/secret-rotation.md`.

## Acceptance criteria

- [ ] First drill executed and signed off in `docs/runbooks/secret-rotation.md`.
- [ ] Any drift found during the drill is patched in the runbook.

## References

- Runbook: `docs/runbooks/secret-rotation.md`
- Drill template: `.github/ISSUE_TEMPLATE/rotate-secrets-quarterly.md`
- Existing CLI: `app/cli/rotate_github_tokens.py`
