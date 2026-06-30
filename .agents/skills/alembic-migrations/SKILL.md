---
name: alembic-migrations
description: Create, review, and apply Alembic migrations for the Ratatoskr Postgres schema. Trigger keywords -- migration, alembic, schema change, new column, new table, ALL_MODELS, migrate_db, autogenerate, downgrade.
version: 1.0.0
allowed-tools: Bash, Read, Write, Edit
---

# Alembic Migrations

Workflow for adding or evolving SQLAlchemy 2.0 models in the Ratatoskr Postgres schema.

## Where Models Live

Models are grouped by area under `app/db/models/`:

| Module | Models |
| ------ | ------ |
| `core.py` | `User`, `Chat`, `Request`, `TelegramMessage`, `CrawlResult`, `LLMCall`, `Summary`, `SummaryEmbedding`, `VideoDownload`, `AudioGeneration`, `AttachmentProcessing`, `UserDevice`, `RefreshToken`, `ClientSecret`, ... |
| `aggregation.py` | `AggregationSession`, `AggregationSessionItem` |
| `batch.py` | `BatchSession`, `BatchSessionItem` |
| `collections.py` | `Collection`, `CollectionItem`, `CollectionCollaborator`, `CollectionInvite` |
| `digest.py` | `Channel`, `ChannelSubscription`, `ChannelPost`, `ChannelPostAnalysis`, ... |
| `repository.py` | `Repository`, `RepositoryEmbedding`, `UserGitHubIntegration` |
| `rss.py` | `RSSFeed`, `RSSFeedSubscription`, `RSSFeedItem`, `RSSItemDelivery` |
| `rules.py` | `WebhookSubscription`, `AutomationRule`, `RuleExecutionLog`, `ImportJob`, `UserBackup` |
| `signal.py` | `Source`, `Subscription`, `FeedItem`, `Topic`, `UserSignal` |
| `topic_search.py` | `TopicSearchIndex` (Postgres TSVECTOR + GIN) |
| `user_content.py` | `SummaryFeedback`, `CustomDigest`, `SummaryHighlight`, ... |

Pick the area that matches the domain; create a new module only when adding an actual new subsystem.

## Workflow

### 1. Add or modify the model

Edit the relevant file under `app/db/models/`. Re-export new models from `app/db/models/__init__.py` so they land in `ALL_MODELS`:

```python
# app/db/models/__init__.py
from .core import Request, User, ...
ALL_MODELS = (User, Request, ..., NewModel)
```

If `ALL_MODELS` is missing the new model, Alembic autogenerate will silently skip it.

### 2. Generate the revision

```bash
source .venv/bin/activate
alembic revision --autogenerate -m "<short summary>"
```

Revision file lands in `app/db/alembic/versions/`. Filename pattern is `<rev_id>_<slug>.py`.

### 3. Hand-review the diff

Autogenerate is good but not perfect. Always check:

- **Enums**: Postgres enum types must be created with `op.execute(...)` BEFORE the column referencing them, and dropped AFTER. Autogenerate often misses this.
- **Indexes**: Confirm names match existing conventions (`ix_<table>_<col>`).
- **Defaults**: Server defaults vs Python defaults -- autogenerate may pick the wrong one.
- **Renames**: Autogenerate treats renames as drop+add. Manually rewrite to `op.alter_column(..., new_column_name=...)` to preserve data.
- **Foreign keys**: Check `ondelete` cascade semantics.

### 4. Apply locally

```bash
python -m app.cli.migrate_db
```

This runs `alembic upgrade head` against `DATABASE_URL`. Run it twice to confirm it's idempotent.

### 5. Verify against the live schema

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "\d <new_table>"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT version_num FROM alembic_version;"
```

### 6. Update docs

CLAUDE.md mandates documenting schema changes in `docs/SPEC.md` (Data Model section). Skipping this guarantees the next reader will miss the change.

## Common Patterns

### Adding a Postgres enum

```python
from alembic import op

attempt_trigger = sa.Enum(
    "initial", "user_retry", "auto_backfill", "repair_loop", "stream_fallback_retry",
    name="llm_attempt_trigger",
    create_type=False,
)

def upgrade() -> None:
    attempt_trigger.create(op.get_bind(), checkfirst=True)
    op.add_column("llm_calls", sa.Column("attempt_trigger", attempt_trigger, nullable=False, server_default="initial"))

def downgrade() -> None:
    op.drop_column("llm_calls", "attempt_trigger")
    attempt_trigger.drop(op.get_bind(), checkfirst=True)
```

### Adding an index after the fact

```python
def upgrade() -> None:
    op.create_index("ix_requests_paper_canonical_id", "requests", ["paper_canonical_id"], unique=False)
```

## Key Files

- **Models**: `app/db/models/<area>.py`
- **Registry**: `app/db/models/__init__.py` (`ALL_MODELS`)
- **Session manager**: `app/db/session.py` (`Database`, sole DB entry point)
- **Alembic env**: `app/db/alembic/env.py`
- **Revisions**: `app/db/alembic/versions/`
- **Apply CLI**: `app/cli/migrate_db.py`
- **Schema doc**: `docs/SPEC.md`

## Important Notes

- Never edit a committed revision file -- write a new one.
- Postgres MVCC handles write concurrency -- no application-level locking needed.
- `Database` (`app/db/session.py`) is the sole DB entry point -- don't open ad-hoc sessions in adapters.
- Migrations run inside the Docker image at boot via `app/cli/migrate_db.py` -- the prod path is the same as the dev path.
- For multi-step destructive changes (column drop, type change), prefer a sequence: add new -> backfill -> swap -> drop old, each as its own revision.
