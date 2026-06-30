---
name: repo-task-board
description: Use when creating, updating, triaging, or completing repository tasks stored as Obsidian Tasks Markdown lines with #task, #status/*, #repo/ratatoskr, and #area/* tags. Use for docs/tasks/*.md, Kanban board maintenance, backlog grooming, and agent-ready implementation planning.
---

# Repository Task Board — ratatoskr

This repository uses Obsidian Tasks-compatible Markdown checkboxes as the canonical task system.

## Canonical task line

```md
- [ ] #task <imperative task title> #repo/ratatoskr #area/<area> #status/<status> <priority>
```

## Allowed statuses

- `#status/backlog`
- `#status/todo`
- `#status/doing`
- `#status/review`
- `#status/blocked`
- `#status/done`
- `#status/dropped`

## Priority markers

- `🔺` critical  ·  `⏫` high  ·  `🔼` medium  ·  `🔽` low

## Canonical files

- `docs/tasks/issues/<slug>.md` — **source of truth** — one file per task with YAML frontmatter + canonical line + spec
- `docs/tasks/backlog.md` — Obsidian Tasks query view for `#status/backlog`
- `docs/tasks/active.md` — Obsidian Tasks query view for `#status/doing` and `#status/review`
- `docs/tasks/blocked.md` — Obsidian Tasks query view for `#status/blocked`
- `docs/tasks/dashboard.md` — full query hub + Bases view links
- `docs/tasks/board.md` — Kanban board (visual only; source of truth is `issues/`)
- `docs/ROADMAP_PRIORITIES.md` — cross-project roadmap (strategic, not per-task)

## Rules

1. Preserve valid Obsidian Tasks syntax.
2. Never create duplicate task lines for the same work.
3. Prefer editing the existing task line over adding a new one.
4. Keep task titles imperative and implementation-oriented.
5. Exactly one `#status/*` tag per task; remove the previous one when transitioning.
6. Add `#blocked` alongside `#status/blocked`; add an indented reason below.
7. When completing: change `[ ]` to `[x]`, set `#status/done`, add `✅ YYYY-MM-DD`.
8. `docs/ROADMAP_PRIORITIES.md` is a strategic planning document — task details go in `docs/tasks/`.
9. Do not change unrelated prose, code, or other sections.

## Per-task notes

Each task lives in its own file at `docs/tasks/issues/<slug>.md` (kebab-case imperative title). This is the source of truth.

### YAML frontmatter schema

```yaml
---
title: Imperative task title
status: doing          # backlog | todo | doing | review | blocked | done | dropped
area: auth             # auth | api | kmp | sync | ci | frontend | observability | testing | content | scraper | llm | db | docs | ops
priority: high         # critical | high | medium | low
owner: Role name
blocks: []
blocked_by: []
created: YYYY-MM-DD
updated: YYYY-MM-DD
---
```

### Canonical line inside the per-task note

The per-task note body contains exactly one checkbox line (the canonical task line), followed by spec sections:

```md
- [ ] #task <title> #repo/ratatoskr #area/<area> #status/<status> <priority>
```

The Tasks plugin picks up this line when querying across the vault — stored inside the per-task note, not in `active.md`/`backlog.md`/`blocked.md`.

### Lifecycle

1. **Create** — use Templater: "Create new note from template" → `new-task.md` in `docs/tasks/templates/`. Fill prompts (title, area, priority, owner). Filename is kebab-case of the title.
2. **Transition** — update `status:` frontmatter field AND update `#status/*` tag in the canonical line. Always update `updated: YYYY-MM-DD`.
3. **Complete / drop** — delete `docs/tasks/issues/<slug>.md`. Git history is the audit trail.
4. **Blocked** — add `#blocked` after `#status/blocked` in the canonical line; add an indented reason bullet below it; populate `blocked_by:` frontmatter with filename stems.

### Index files are query-only

`active.md`, `backlog.md`, `blocked.md`, and `dashboard.md` contain only Obsidian Tasks ` ```tasks ` query blocks. Do NOT add task lines to these files; add them only inside `issues/<slug>.md`.

## Task creation workflow

1. Search `docs/tasks/` for similar tasks.
2. If similar task exists, update it instead of duplicating.
3. Choose the right file: `backlog.md` for new work, `active.md` if starting now.
4. Assign: `#repo/ratatoskr`, `#area/<area>`, one `#status/*`, priority marker.
5. Add context as indented bullets only when acceptance criteria is non-obvious.
