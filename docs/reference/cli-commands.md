# CLI commands

Ratatoskr exposes operational entry points as Python modules under `app/cli/`. Run them from the repository root with the project virtual environment activated. Each command's `--help` output is the authoritative option reference.

## Current modules

| Command | Purpose |
|---|---|
| `python -m app.cli.ai_backup` | Run AI account backup operations. |
| `python -m app.cli.backfill_embeddings` | Backfill summary embeddings. |
| `python -m app.cli.backfill_repository_embeddings` | Backfill repository embeddings. |
| `python -m app.cli.backfill_vector_store` | Populate supported Qdrant entities from PostgreSQL. |
| `python -m app.cli.check_userbot_session` | Check the Telethon digest userbot session. |
| `python -m app.cli.healthcheck` | Probe application dependencies for container health checks. |
| `python -m app.cli.init_userbot_session` | Initialize the digest userbot session interactively. |
| `python -m app.cli.mcp_server` | Start the MCP server over its configured transport. |
| `python -m app.cli.migrate_db` | Render pending Alembic SQL; add `--apply` to mutate the schema. |
| `python -m app.cli.reconcile_vector_index` | Detect or repair PostgreSQL/Qdrant drift. |
| `python -m app.cli.repository` | Run repository-ingestion operations. |
| `python -m app.cli.requeue_failed_task` | Requeue a persisted failed Taskiq job. |
| `python -m app.cli.retry` | Retry a failed request workflow. |
| `python -m app.cli.rotate_github_tokens` | Re-encrypt stored GitHub tokens and browser sessions with a new primary key. |
| `python -m app.cli.search` | Search indexed summaries from the terminal. |
| `python -m app.cli.seed_demo_data` | Seed development/demo data. |
| `python -m app.cli.signal_eval` | Evaluate signal-personalization behavior. |
| `python -m app.cli.summary` | Process and summarize one URL through the production graph. |
| `python -m app.cli.sync_github_stars` | Run GitHub star synchronization once. |
| `python -m app.cli.taskiq_worker` | Start the Taskiq worker or scheduler entry point. |

## Common examples

```bash
python -m app.cli.summary --url https://example.com/article
python -m app.cli.migrate_db --apply
python -m app.cli.reconcile_vector_index --help
python -m app.cli.taskiq_worker --help
```

The former SQLite-era utilities `cleanup_embeddings`, `rebuild_indexes`, and `migrate_sqlite_to_postgres` no longer exist. Use Alembic for schema evolution and the vector reconciliation/backfill commands for derived indexes.
