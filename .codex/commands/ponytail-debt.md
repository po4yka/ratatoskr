---
description: Harvest ponytail comments into a tracked debt ledger
codex-trigger: "@ponytail-debt"
claude-equivalent: "/ponytail-debt"
---

Harvest every `ponytail:` comment in this repository into a debt ledger so deferrals do not rot into "later means never". Search the whole tree for comment markers, skipping `node_modules`, `.git`, and build output. One row per marker, grouped by file: `<file>:<line> - <what was simplified>. ceiling: <the limit named in the comment>. upgrade: <the trigger to revisit>.` Tag any marker that names no upgrade path or trigger as `no-trigger`. End with the count of markers and how many lack a trigger. If none: `No ponytail: debt. Clean ledger.` Report only, change nothing.
