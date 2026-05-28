---
title: Offload blocking backup I/O to threads
status: backlog
area: ops
priority: high
owner: unassigned
epic: epic-eliminate-event-loop-blocking
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Offload blocking backup I/O to threads #repo/ratatoskr #area/ops #status/backlog ⏫

## Objective

Backup-path code runs blocking work on the event loop. `pg_dump` via `subprocess.run` is correctly wrapped in `to_thread` on the Telegram path but NOT on the API archive path; `verify_backup` reads whole backup files with blocking `Path.read_bytes()`; backup cleanup scans a directory synchronously after `to_thread` returns.

## Context (evidence)

- `app/db/runtime/backup.py:47` and `:69` (`subprocess.run(...)` for pg_dump)
- `app/infrastructure/persistence/backup_archive_service.py:313` (`async_create_backup_archive` calls create_backup_copy without `to_thread`)
- `app/api/routers/backups.py:302` (`payload = Path(file_path).read_bytes()` inside `async def verify_backup`)
- `app/adapters/telegram/telegram_bot.py:366-368` (sync `_cleanup_old_backups` iterdir/stat on the loop)

## Scope

- Wrap `create_backup_copy` in `asyncio.to_thread` at the `async_create_backup_archive` call site (or switch `DatabaseBackupService` to `asyncio.subprocess.create_subprocess_exec`)
- Change `verify_backup` to `await asyncio.to_thread(Path(file_path).read_bytes)`
- Move cleanup into the `to_thread` call

## Acceptance criteria

- [ ] No `subprocess.run`, `read_bytes`, or directory scan executes on the event loop in the backup paths
- [ ] An async test confirms a large backup verify does not stall a concurrent coroutine

## Epic

Part of [[epic-eliminate-event-loop-blocking]].

## References

- Performance audit findings H-1, H-2, L-1 (2026-05-28).
