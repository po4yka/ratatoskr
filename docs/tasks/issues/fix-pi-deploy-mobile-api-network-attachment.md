---
title: Fix mobile-api pi-deploy losing docker_default network attachment
status: backlog
area: ops
priority: high
owner: unassigned
blocks: []
blocked_by: []
created: 2026-05-24
updated: 2026-05-24
---

- [ ] #task Fix mobile-api pi-deploy losing docker_default network attachment #repo/ratatoskr #area/ops #status/backlog ⏫

## Objective

`tools/scripts/build-and-deploy-pi.sh:153` runs `compose up -d --no-deps --force-recreate ${SERVICE}`. For `SERVICE=mobile-api` on the Pi, this leaves the recreated container attached to **only** `firecrawl_internal`, dropping `docker_default`. Mobile-api then cannot reach `postgres`, `redis`, or the host-native qdrant via `host-gateway`; the container crash-loops silently with `die exit=1` every ~17 s (no traceback — the failure is at TCP-connect time, before any user-visible logging path). Bot / worker / scheduler are not affected — they don't run `migrate_db && uvicorn` and would attach the same way, but their working subset of network targets happens to fall on `firecrawl_internal`.

## Repro

1. `make pi-deploy SERVICE=mobile-api` (or invoke the script directly with `--service mobile-api`).
2. `ssh raspi 'docker inspect ratatoskr-mobile-api --format "{{range \$k, \$v := .NetworkSettings.Networks}}{{\$k}} {{end}}"'` → prints only `firecrawl_internal`.
3. Container reports `(health: starting)` (the healthcheck script `app.cli.healthcheck` connects to DB via the postgres alias — fails — but the cycle is fast enough that the healthcheck-as-database-probe sometimes passes intermittently before the container dies, depending on timing of the DB lookup).
4. `docker logs` stops at `setup plugin alembic.autogenerate.comments` and never reaches `alembic_upgrade_complete`.

Confirmed during 2026-05-24 redeploy. Workaround: full `compose up -d --force-recreate mobile-api` (no `--no-deps`) — restarts postgres + redis + migrate as part of the dep chain, which is mildly disruptive but reliably reattaches both networks.

## Context

- Merged compose (`docker compose ... config`) declares both networks for `mobile-api`: `default: {}` and `firecrawl_internal: {}` — the intent is correct.
- The Pi overlay `ops/docker/docker-compose.pi.yml:131-142` pins the default network to a stable external-style name (`networks.default.name: docker_default`) and declares `firecrawl_internal` as `external: true`.
- Bot has both networks attached after the same deploy flow (`docker_default=172.27.0.9, firecrawl_internal=172.27.0.x`). Mobile-api ends up with only the `external: true` one.
- Plausible cause: known Docker Compose interaction between `--no-deps` and a mix of `external: true` + project-managed networks, where the project network gets skipped because compose decides not to manage it during a `--no-deps` recreate. This needs reproduction with a minimal compose file to confirm.

## Scope

- Reproduce the bug in isolation (minimal two-network compose file + `--no-deps --force-recreate`).
- Decide on fix: either (a) drop `--no-deps` for `mobile-api` only in `build-and-deploy-pi.sh`, accepting the postgres / redis restart cost; (b) follow the recreate with an explicit `docker network connect docker_default <container>`; or (c) restructure the Pi network topology so mobile-api only needs one network.
- Update `tools/scripts/build-and-deploy-pi.sh` to apply the chosen fix.
- Document the chosen behavior in `.claude/skills/pi-deploy/SKILL.md`.

## Acceptance criteria

- [ ] `make pi-deploy SERVICE=mobile-api` from a clean state leaves `ratatoskr-mobile-api` attached to both `docker_default` and `firecrawl_internal`.
- [ ] Container reaches `(healthy)` within the existing start_period budget without crash-looping.
- [ ] `/healthz` and `/web/` return 200 after the script returns.
- [ ] No regression for `SERVICE=ratatoskr`, `worker`, `scheduler`, `mcp*`.

## References

- Script: `tools/scripts/build-and-deploy-pi.sh:146-161`
- Pi overlay network declaration: `ops/docker/docker-compose.pi.yml:131-142`
- Mobile-api networks in base compose: `ops/docker/docker-compose.yml:300-395` (search `mobile-api:` → `networks:`)
- Skill: `.claude/skills/pi-deploy/SKILL.md`
