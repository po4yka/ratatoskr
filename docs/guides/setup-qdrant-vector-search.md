# Set up Qdrant vector search

Qdrant provides semantic and hybrid retrieval for supported entity types. PostgreSQL remains authoritative; Qdrant is a derived index maintained by synchronous fast-path writes and the Taskiq reconciler.

## Start Qdrant

```bash
docker compose -f ops/docker/docker-compose.yml up -d qdrant
curl -fsS http://127.0.0.1:6333/readyz
```

Compose points application services at `http://qdrant:6333`. Do not expose the Qdrant API publicly; configure an API key/TLS when using a remote service.

## Configure embeddings

Choose the embedding provider and model in `ratatoskr.yaml` or the matching environment settings. Provider/model/dimension/collection-version/environment/user-scope form the effective embedding space. Changing them can make existing vectors stale or incompatible.

Review [Environment Variables](../reference/environment-variables.md) and [Vector Index Synchronization](../vector-index-sync.md) before switching models or dimensions.

## Inspect drift

```bash
python -m app.cli.reconcile_vector_index
```

The reconciler reports supported PostgreSQL/Qdrant inconsistencies. Run a bounded dry repair before a broad write when scope is uncertain:

```bash
python -m app.cli.reconcile_vector_index --repair --dry-run --limit=100
python -m app.cli.reconcile_vector_index --repair --limit=100
```

For a deliberate full summary backfill:

```bash
python -m app.cli.backfill_vector_store --force
```

Repository embeddings have their own backfill module; the reconciliation command invokes both summary and repository repair paths where configured.

## Verify search

Create or identify a summary with enough stored text, wait for or run reconciliation, then use the API/CLI search surface with a query that should match semantically. Confirm:

- the PostgreSQL embedding/index metadata is current;
- the Qdrant point uses the expected deterministic ID and entity type;
- user/environment/collection/embedding-space filters match;
- hydration still applies the authenticated `user_id` guard.

## Back up and recover

Qdrant snapshots or a stopped storage-volume backup provide the fastest exact recovery. When unavailable or incompatible after a model/namespace change, rebuild supported points from PostgreSQL. See [Back Up and Restore](backup-and-restore.md).

## Troubleshooting

A healthy HTTP endpoint does not prove dimensions or namespaces match. Inspect application logs and the reconciliation report for unavailable store, wrong collection, dimension mismatch, stale content hash, missing point, or orphan point.

See [Troubleshooting — Qdrant](../reference/troubleshooting.md#qdrant-issues).

Last audited: 2026-07-15.
