# How to Correctly Integrate and Use CocoIndex (ratatoskr)

> Scope: this document fixes ratatoskr's broken CocoIndex integration (`app/infrastructure/cocoindex/runtime.py`, `flow.py`, `embedding_bridge.py`). Every API claim is sourced inline. Where the research is uncertain, it says so explicitly. Pin under discussion: `cocoindex>=1.0.3,<1.1` (`pyproject.toml` line 307).
>
> _Source: web research (CocoIndex docs, GitHub `cocoindex-io/cocoindex` @ v1.0.10, PyPI) cross-checked against the installed `cocoindex 1.0.3` in this repo's venv. Generated 2026-06-17; not committed by default._

---

## 1. Verdict on the version mismatch

**What ratatoskr's code targets:** the CocoIndex **v0 "flow" API** — `@cocoindex.flow_def`, `cocoindex.FlowBuilder` / `DataScope`, `flow_builder.add_source(cocoindex.sources.Postgres(...))`, `data_scope.add_collector()`, `collector.collect()` / `.export()`, `cocoindex.targets.Qdrant(...)`, `cocoindex.init(cocoindex.Settings(...))`, and `cocoindex.FlowLiveUpdater(flow, FlowLiveUpdaterOptions(live_mode=True))`. These are the symbols in `runtime.py` and `flow.py`.

**What PyPI `cocoindex` 1.0.3 actually is:** the **v1 "App / reactive-component" API**. v1.0.0 (2026-04-22) was a complete breaking rewrite. Internal state moved from Postgres to a local LMDB file. **Every v0 symbol the code uses was deleted.** Verified against the live `__all__` export list — `flow_def`, `FlowBuilder`, `DataScope`, `DataSlice`, `add_collector`, `collect`, `export`, `sources`, `targets`, `storages`, `init`, `FlowLiveUpdater`, `FlowLiveUpdaterOptions` are **not present**.
- Source `__all__`: https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/_internal/api.py
- Installed-package confirmation (this repo): `.venv/.../cocoindex-1.0.3.dist-info/METADATA` shows `Version: 1.0.3`, `msgspec>=0.19.0` (the v1 fingerprint), `watchfiles>=1.1.0`.

**The other half of the mismatch — `Settings(database_url=..., target_schema='cocoindex')`:** this signature exists in **no version of CocoIndex, ever.**
- v1 `Settings.__init__(db_path=None, db_settings=None, *, lmdb_max_dbs=None, lmdb_map_size=None, global_execution_options=None)` — LMDB only, no Postgres. Passing `database_url=` raises `TypeError: Settings.__init__() got an unexpected keyword argument 'database_url'`. Source: https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/_internal/setting.py
- The v0 form was `cocoindex.init(cocoindex.Settings(database=cocoindex.DatabaseConnectionSpec(url=...), db_schema_name="cocoindex"))` — note `database=` (nested spec) and `db_schema_name=`, **never** `database_url=` and **never** `target_schema=`. The `target_schema` keyword never existed in any version. Source (v0 docs): https://cocoindex.io/docs-v0/core/settings/

**Net result:** the integration is dead code under the current pin. The moment `RATATOSKR_COCOINDEX_ENABLED=1` is set, `runtime.py` raises `AttributeError`/`TypeError` at startup. It is invisible only because the feature is off and CI injects a fake `cocoindex` into `sys.modules` (see §6).

### The exact correct pin

There are **two** valid resolutions. Pick by whether you keep the code's current API shape or rewrite it.

| Goal | Pin | Consequence |
|---|---|---|
| **Keep the existing `flow_def`/`sources`/`targets`/`FlowLiveUpdater` code as-written** | `cocoindex>=0.3.0,<1.0.0` (last v0 release: `0.3.39`, 2026-04-29) | Resurrects the v0 API the code already uses. But **v0 requires Postgres as CocoIndex's own state backend** and the `Settings(database_url=, target_schema=)` call is *still wrong* and must be fixed to `Settings(database=DatabaseConnectionSpec(url=...), db_schema_name=...)`. v0 is maintenance-only; docs live at https://cocoindex.io/docs-v0/. PyPI history: https://pypi.org/project/cocoindex/#history |
| **Stay on the current pin (v1) and modernize the code** | Keep `cocoindex>=1.0.3,<1.1` (latest in range: `1.0.10`, 2026-06-14) | The v0 API is **gone**, not deprecated-with-shim. The code must be rewritten to the `App` / `@coco.fn` / `mount_each` / connector-target model (§2). LMDB replaces the Postgres state backend. |

**Recommendation on the pin:** do **not** pin back to v0. v0 is in maintenance mode, requires a second Postgres schema for engine state, and the v1 Postgres source has *no change-capture yet* (§3) — so even a "correct" v0 build buys low-latency sync that v1 can't match, for a redundant writer ratatoskr doesn't need (§7). Keep `>=1.0.3,<1.1` and either rewrite to v1 or remove the integration. Note one transitive caveat if you bump within the range: `1.0.3` uses `watchfiles>=1.1.0`, `1.0.10` swaps to `watchdog>=6.0.0` (a different, Python-based watcher) — ratatoskr's own code imports neither, so this is transitive-only, but it is a real core-dep replacement, not cosmetic. (https://pypi.org/pypi/cocoindex/json)

---

## 2. Correct current API (v1, `cocoindex>=1.0.3,<1.1`)

The v1 mental model is inverted from v0: there is no declarative flow graph and no `init()`. You write an **async `main` function** that *mounts components*; each component is a `@coco.fn` that declares target rows/points imperatively. State lives in LMDB; Postgres and Qdrant are **connectors** (sources/targets), not the engine backend.

### Init / lifecycle

```python
import pathlib
import cocoindex as coco

@coco.lifespan
async def coco_lifespan(builder: coco.EnvironmentBuilder):
    builder.settings.db_path = pathlib.Path("/data/cocoindex.db")  # LMDB state file
    # builder.settings.lmdb_map_size = 8 * 1024**3   # bump on "map full"
    builder.provide(PG_DB, pool)          # shared resources via ContextKey
    builder.provide(QDRANT, qdrant_client)
    builder.provide(EMBEDDER, embedder)
    yield                                  # teardown after yield
```
There is **no `cocoindex.init()`** and **no `database_url`**. Confirmed: https://cocoindex.io/docs/programming_guide/app/ and the migration table at https://github.com/cocoindex-io/cocoindex/blob/main/skills/cocoindex/SKILL.md

> Caveat (version-specific, verified against installed `1.0.3`): in **1.0.3** the `LmdbSettings` class and the `db_settings=` kwarg/attribute do **not exist** — set `builder.settings.lmdb_max_dbs` / `builder.settings.lmdb_map_size` directly on `Settings`. `LmdbSettings` appears in a later 1.0.x release and in the current docs. (Installed source: `.venv/.../cocoindex/setting.py`.)

### Postgres source (v1)

```python
from cocoindex.connectors import postgres

source = postgres.PgTableSource(
    pool,                      # asyncpg.Pool you own (from use_context or lifespan)
    table_name="summaries",
    pg_schema_name="public",   # default "public"
    row_type=SummaryRow,       # OR row_factory=...; mutually exclusive
)
async for key, row in source.fetch_rows().items(lambda r: (r.id,)):
    ...
```
**Critical:** the v1 `PgTableSource` constructor has **no `ordinal_field`, no `ordinal_column`, no `primary_key_fields`, no `notification`, no `refresh_interval`.** It is a plain async row reader. The docstring says "Change notifications will be added later." ratatoskr's `flow.py` passes `ordinal_field='updated_at'` and `primary_key_fields=[...]` to a v0 `sources.Postgres` — neither argument survives into v1. Source: https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/connectors/postgres/_source.py

### Embed (use a `@coco.fn`, not a side-channel bridge — see §4)

```python
from cocoindex.ops.sentence_transformers import SentenceTransformerEmbedder

EMBEDDER = coco.ContextKey[SentenceTransformerEmbedder]("embedder", detect_change=True)

@coco.fn(memo=True)
async def process_summary(row: SummaryRow, target) -> None:
    vec = await coco.use_context(EMBEDDER).embed(row.text)   # built-in, batched, memoized
    target.declare_point(qdrant.PointStruct(id=row.point_id, vector=vec.tolist(),
                                             payload={...}))
```
`SentenceTransformerEmbedder.embed()` is `@coco.fn(memo=True, version=1)`; its batched `_embed` runs on `coco.GPU` runner with `max_batch_size=64`. Source: https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/ops/sentence_transformers.py

### Collect + export to Qdrant (v1)

In v1 there is no `collector.collect()`/`.export()`. You **mount a collection target** and call `.declare_point()` inside the component:

```python
from cocoindex.connectors import qdrant

QDRANT = coco.ContextKey[qdrant.QdrantClient]("qdrant")

@coco.fn
async def app_main() -> None:
    target = await qdrant.mount_collection_target(
        QDRANT,
        collection_name="summary_embeddings",
        schema=await qdrant.CollectionSchema.create(          # NOTE: async
            vectors=qdrant.QdrantVectorDef(schema=EMBEDDER, distance="cosine"),
        ),
    )
    source = postgres.PgTableSource(pool, table_name="summaries", row_type=SummaryRow)
    await coco.mount_each(
        process_summary,
        source.fetch_rows().items(lambda r: (r.id,)),
        target,
    )

app = coco.App(coco.AppConfig(name="RatatoskrSummaries"), app_main)
```
Signatures verified: `mount_collection_target(db, collection_name, schema, *, managed_by=...)`, `CollectionTarget.declare_point(point)`, `qdrant.PointStruct` re-export. `CollectionSchema.create(...)` is **async** (`await` required). Sources: https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/connectors/qdrant/_target.py and the end-to-end example https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/examples/text_embedding_qdrant/main.py

> `ratatoskr` writes one point per entity with a deterministic `uuid5` id. That maps cleanly: compute the id in Python and pass it as `PointStruct(id=<uuid5>, ...)`. No `primary_key_fields=['id']` export arg exists in v1 — the point id *is* the key.

### Run

```python
app.update_blocking()          # one-shot catch-up (sync; NOT inside a running loop)
await app.update()             # one-shot async
await app.update(live=True)    # live mode (see §3)
```
`App.update(*, full_reprocess=False, live=False, preview=False) -> UpdateHandle` and `update_blocking(*, report_to_stdout=False, ...)`. `update_blocking` raises `RuntimeError` if called from inside a running asyncio loop. Source: https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/_internal/app.py

---

## 3. Change capture & live updates done right

**The blunt truth for v1: there is no Postgres change capture.** As of 1.0.10, the Postgres connector has no LISTEN/NOTIFY, no ordinal/watermark column, and no `refresh_interval`. There is no Postgres `LiveMapView`/`LiveMapFeed`. Low-latency CDC from Postgres is **architecturally absent** in v1. Source (connector docstring + docs): https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/connectors/postgres/_source.py and https://cocoindex.io/docs/connectors/postgres

So in v1 your only incremental options are:

1. **One-shot polling on a schedule** (`app.update()` re-run on a timer you own — e.g. a Taskiq cron). Memoization (`@coco.fn(memo=True)`) skips unchanged rows, so re-scans are cheap on the embed side but still read every row from Postgres.
2. **`coco.auto_refresh(process_fn, interval=timedelta(...))`** inside a live app. In live mode it re-runs `process_fn` every `interval` (fixed *delay between cycles*, never overlapping); in catch-up mode it runs once and exits. This is the closest v1 equivalent to v0's `refresh_interval`. Source: https://cocoindex.io/docs/programming_guide/live_mode/

```python
@coco.fn
async def app_main() -> None:
    await coco.mount(
        coco.auto_refresh(sync_summaries, interval=datetime.timedelta(seconds=30)),
        ...,
    )
app.update_blocking(live=True)   # keeps running; sync_summaries fires every 30s
```

There is **no v1 equivalent of v0's `next_status_updates()`** for reacting to per-source change events. `UpdateHandle.watch()` streams *progress/stats* snapshots (`UpdateSnapshot`), not change events. Reactive logic must live inside `auto_refresh` cycles or a `LiveComponent`. Source: https://cocoindex.io/docs/advanced_topics/progress_monitoring/

If you genuinely need LISTEN/NOTIFY-driven low-latency sync, that is a **v0-only** capability (`cocoindex.sources.Postgres(notification=PostgresNotification(channel_name=...))`, which auto-creates the trigger function `{channel}_n` and trigger `{channel}_t`). Source: https://cocoindex.io/docs-v0/sources/postgres/

### Fixing ratatoskr's dead knobs

All four are dead because they were never threaded into any CocoIndex call, and **none of them maps onto a real v1 parameter**:

| Dead knob | v1 reality | Action |
|---|---|---|
| `RATATOSKR_COCOINDEX_POLL_INTERVAL_SEC` (30) | No v1 source polling param. Closest is `auto_refresh(interval=...)` — but that's *your* timer, not a cocoindex source setting. | **Delete** the knob; if you keep CocoIndex, drive cadence from your own Taskiq cron or `auto_refresh`. |
| `RATATOSKR_COCOINDEX_BATCH_SIZE` (32) | Embedding batch size is owned by the embedder (`SentenceTransformerEmbedder._embed(max_batch_size=64)`), not a flow-level setting. | **Delete**; if needed, configure it on the embedder instance. |
| `RATATOSKR_COCOINDEX_POOL_MAX` (4) | v1 does **not** own a Postgres pool. The caller creates and sizes `asyncpg.create_pool(dsn, min_size=, max_size=)`. (`psycopg`/the `psycopg[binary]>=3.3.4` extra is a v0-era artifact — v1 Postgres connector uses **asyncpg**.) | **Delete**; size your own asyncpg pool in the lifespan. |
| `RATATOSKR_COCOINDEX_LISTEN_CHANNEL` | No LISTEN in v1. (It was passed to `build_summaries_flow` but unused, and never passed to the repo flow.) | **Delete.** |

### What migration 0007 must actually create

**Nothing — and that's the bug-or-non-bug to resolve by direction:**
- **If you stay on v1:** migration 0007 should create **no NOTIFY trigger at all**, because v1 has no LISTEN/NOTIFY consumer. The `GRANT TRIGGER` it currently issues is harmless but pointless; you can drop it. v1 keeps its watermark/memo state in **LMDB**, not Postgres — there is no Postgres state schema to provision. (Internal storage: https://cocoindex.io/docs/advanced_topics/internal_storage/)
- **If you ever went back to v0 with `PostgresNotification`:** 0007 is *insufficient*. v0's notification auto-creates the trigger **function + trigger** itself, but only if the role has `TRIGGER` *and* `CREATE` on the schema — and the watermark column (`ordinal_column`) must exist and be indexed. Granting `TRIGGER` alone (as 0007 does) does not create the NOTIFY trigger; the comment-assumption that "cocoindex installs it" is only true under v0 *and* with sufficient DDL privileges.

---

## 4. Embedding without double-loading the model

**The `embedding_bridge.py` approach is unnecessary and actively harmful on the Pi.** It spins up a *second* asyncio loop, a *second* embedding service (its own `create_embedding_service()` → possibly a second sentence-transformers model load), and a *second* `EmbeddingCache(RedisCache(...))` with its own Redis pool — a full duplicate of the app's embedding stack. On a 16 GB Raspberry Pi, a second local sentence-transformers model is exactly the kind of memory you can't spare, and the custom synchronous bridge exists only because v0 needed `embed_text_sync()` to fit inside a `data_scope.row()` block.

In **v1 the bridge has no reason to exist.** The correct pattern shares **one** embedder via a `ContextKey` provided once in the lifespan, and calls it with `await coco.use_context(EMBEDDER).embed(text)` inside an async `@coco.fn`. CocoIndex's runner handles batching and offloads the sync `model.encode()` to a thread/GPU runner for you — no hand-rolled daemon loop, no second cache.

```python
EMBEDDER = coco.ContextKey[SentenceTransformerEmbedder]("embedder", detect_change=True)

@coco.lifespan
async def coco_lifespan(builder):
    builder.provide(EMBEDDER, SentenceTransformerEmbedder("sentence-transformers/all-MiniLM-L6-v2"))
    yield

@coco.fn(memo=True)
async def process_summary(row, target):
    vec = await coco.use_context(EMBEDDER).embed(row.text)   # one model, batched, memoized
    target.declare_point(qdrant.PointStruct(id=row.point_id, vector=vec.tolist(), payload={...}))
```
Sources: canonical lifespan+ContextKey pattern and `SentenceTransformerEmbedder` class — https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/ops/sentence_transformers.py ; live-mode/lifespan — https://cocoindex.io/docs/programming_guide/app/

**Can you reuse ratatoskr's *existing* embedding service instead of cocoindex's `SentenceTransformerEmbedder`?** Yes — wrap it as a plain `@coco.fn`:
```python
@coco.fn(memo=True)
async def embed_text(text: str) -> NDArray:
    return await app_embedding_service.embed(text)   # your existing async service
```
`@coco.fn` accepts async functions directly; for a *sync* embedding body that needs batching/GPU, use `@coco.fn.as_async(batching=True, runner=coco.GPU, max_batch_size=...)`. (Verified: `@coco.fn(batching=True)` is for async bodies; `@coco.fn.as_async(batching=True)` is for sync bodies. Source: https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/_internal/function.py)

This collapses the Pi's two model copies into one, drops the second Redis pool, and deletes the entire `embedding_bridge.py` daemon-loop machinery.

> Memoization detail: cocoindex's `SentenceTransformerEmbedder` fingerprints itself via `__coco_memo_key__ = (model_name, device, trust_remote_code)`, so the memo cache is correct without pickling the model. If you wrap your own service, make sure the wrapped `@coco.fn`'s inputs (the text) fully determine the output, or pass config via `deps=`/regular args so the memo key is sound.

---

## 5. Embedding CocoIndex in a long-running async service

ratatoskr is an always-on asyncio process, so use the **async** lifecycle exclusively.

- **Lifecycle:** call `await coco.start()` in the app's startup path and `await coco.stop()` on shutdown. The `@coco.lifespan` setup runs on `start()` (or first `update()`). For background live updates, wrap `app.update(live=True)` in a task:
  ```python
  live_task = asyncio.create_task(app.update(live=True))   # UpdateHandle is awaitable
  # shutdown: live_task.cancel(); await coco.stop()
  ```
  Sources: https://cocoindex.io/docs/programming_guide/app/ , https://cocoindex.io/docs/programming_guide/sdk_overview/
- **Never call the blocking variants from the event loop.** `app.update_blocking()`, `coco.start_blocking()`, and `with coco.runtime()` check `asyncio.get_running_loop()` and raise `RuntimeError` immediately if a loop is running. ratatoskr's `runtime.py` currently calls `flow.setup` and `updater.__enter__` via `asyncio.to_thread` — that whole pattern is v0-shaped and disappears in v1 (use `await app.update(...)`). Verified in the installed 1.0.3 runtime.
- **Threading:** the Rust/PyO3 core runs its own Tokio executor on background threads, separate from the Python loop. Thread count is **not** user-configurable — `COCOINDEX_WORKER_THREADS` does not exist in 1.0.3. Don't add a knob for it.
- **Connection budget:** v1 holds **no internal Postgres pool**. You create and size `asyncpg.create_pool(dsn, min_size=, max_size=)` in the lifespan and `builder.provide(PG_DB, pool)`. On a 16 GB Pi keep `max_size` small (1–3). This is the *real* replacement for the dead `RATATOSKR_COCOINDEX_POOL_MAX`. Source: https://cocoindex.io/docs/connectors/postgres
- **Observability:** there is **no built-in Prometheus/OTel export.** Stats are plain Python objects — pull them from `handle.stats()` (`UpdateStats.total` / `.by_component[...]` with `num_adds`, `num_deletes`, `num_errors`, `num_reprocesses`) or stream `handle.watch()` (`AsyncIterator[UpdateSnapshot]`) and publish to ratatoskr's existing `app/observability/metrics.py`. Source: https://cocoindex.io/docs/advanced_topics/progress_monitoring/
- **LMDB state:** set `db_path` to a *persistent* container path (e.g. under the Pi's data volume), or memo/watermark state is lost on every restart and you re-embed everything. Default `map_size` is ~4 GiB virtual; bump via `lmdb_map_size` on "map full" errors.
- **Pitfall — nested mounts in live tasks:** an internal `_in_process_live` flag forbids calling `coco.mount*` / `coco.use_mount()` from inside a live-processing task body. Keep mounting in `app_main`, not inside per-row `@coco.fn`s.

---

## 6. Concrete fix plan for ratatoskr

Ordered, minimal. Each bullet names the file. (This assumes the *fix* path; §7 argues the *remove* path is likely better.)

1. **Decide direction first** (gates everything below): keep+rewrite to v1, or remove. Do not pin back to v0 (§1).
2. **Rewrite the runtime to the v1 lifecycle** — `app/infrastructure/cocoindex/runtime.py`: delete `cocoindex.init(...)`, `Settings(database_url=, target_schema=)`, `FlowLiveUpdater`, `FlowLiveUpdaterOptions`, and all `asyncio.to_thread(flow.setup)` / `__enter__`/`__exit__` plumbing. Replace with `@coco.lifespan` (set `builder.settings.db_path`, provide `PG_DB`/`QDRANT`/`EMBEDDER` ContextKeys), `coco.App(AppConfig(name=...), app_main)`, and `await coco.start()` / `await app.update(live=True)` / `await coco.stop()`.
3. **Rewrite the flows as `@coco.fn` apps** — `app/infrastructure/cocoindex/flow.py`: replace `@cocoindex.flow_def` + `add_source(sources.Postgres(...))` + `add_collector()` + `collect()`/`export(targets.Qdrant(...))` with `PgTableSource` → `mount_each(process_fn, source.fetch_rows().items(key), target)` → `qdrant.mount_collection_target(...)` + `declare_point(PointStruct(id=<uuid5>, ...))`. Keep the deterministic `uuid5` id computation; drop `ordinal_field`/`primary_key_fields` (no v1 equivalent). (§2)
4. **Delete the embedding bridge** — remove `app/infrastructure/cocoindex/embedding_bridge.py` and the second loop/model/Redis pool. Replace with one `ContextKey[SentenceTransformerEmbedder]` (or a `@coco.fn` wrapping ratatoskr's existing async embedding service) provided once in the lifespan. (§4)
5. **Delete the dead knobs** — `RATATOSKR_COCOINDEX_POLL_INTERVAL_SEC`, `_BATCH_SIZE`, `_POOL_MAX`, `_LISTEN_CHANNEL` in `app/config/*` and `docs/cocoindex.md` / `docs/reference/environment-variables.md`. If you keep a cadence knob, repurpose it as the `auto_refresh(interval=...)` value, clearly owned by ratatoskr, not cocoindex. (§3)
6. **Fix the pin & extras** — `pyproject.toml` (line ~307): keep `cocoindex>=1.0.3,<1.1`, but install the **v1 connector extras** `cocoindex[postgres,qdrant]` (asyncpg + qdrant-client). **Drop `psycopg[binary]>=3.3.4`** unless something else needs it — v1's Postgres connector uses asyncpg, not psycopg.
7. **Fix migration 0007** — if staying v1, remove the `GRANT TRIGGER` (no NOTIFY consumer exists) and add no trigger; document that CocoIndex state is LMDB-on-disk, not Postgres. (§3)
8. **Add a real smoke test that imports the actual package** — new `tests/.../test_cocoindex_smoke.py`: `importorskip("cocoindex")`, then build the real `app` via `build_summaries_flow()`/`app_main` and assert `coco.App(...)` constructs and `app.update_blocking(preview=True)` runs against a throwaway LMDB `db_path` (tmp dir) with a fake/empty source. **This is the test that would have caught the break.** The existing `test_runtime.py` injects a fake `cocoindex` into `sys.modules`, so the v0/v1 API divergence is invisible to CI — the smoke test must run against the **installed** package (CI already does `--extra cocoindex`). Honor the project's CI constraint: graph/extra code is lazy-imported and tests `importorskip`.
9. **Guard startup** — `runtime.py`: on `RATATOSKR_COCOINDEX_ENABLED`, fail loudly with a clear message if `coco.App` is unavailable, rather than `AttributeError` deep in flow construction.

---

## 7. Recommendation: fix vs remove

**Remove CocoIndex from ratatoskr.** Recommendation strength: high.

Three deciding factors:

1. **It is a third, redundant writer to a job already fully covered.** The summarize-graph persist node writes a read-your-writes, byte-identical Qdrant point synchronously (ADR-0012), and the Taskiq reconciler converges every 30 min. CocoIndex adds a *third* path writing the *same* `uuid5`-keyed points with no unique capability. Redundant vector writers are a correctness liability (write-ordering / divergence), not a feature.
2. **v1 can't even do the one thing that would justify it.** The only differentiated value CocoIndex could add over the existing two writers is low-latency CDC. v1 has **no Postgres change capture** (§3) — so on the current (correct) pin you'd get *polling*, which is exactly what the reconciler already does, minus a redundant LMDB state store and (today) a duplicate embedding model on a memory-constrained Pi.
3. **The cost of keeping it is real and ongoing.** A correct fix means rewriting `runtime.py` + `flow.py`, deleting `embedding_bridge.py`, provisioning a persistent LMDB volume on the Pi, sizing a second asyncpg pool, and wiring custom stats→Prometheus — all to run a writer you don't need. The cheap, honest fix is deletion: drop `app/infrastructure/cocoindex/`, the `cocoindex`/`psycopg[binary]` deps, the four dead env knobs, the `--extra cocoindex` CI step, the fake-module test, and the `GRANT TRIGGER` in migration 0007.

**Keep+fix only if** a concrete future requirement appears that the synchronous fast path + reconciler genuinely cannot serve (e.g. a new multi-source ingestion pipeline where CocoIndex's reactive incremental model earns its keep) — and even then, only after v1 ships Postgres change capture ("will be added later," per the connector docstring). Until that day, CocoIndex is dead, redundant code; the lowest-risk, lowest-maintenance action is to remove it.

---

### Source index
- v1 `__all__` / API surface: https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/_internal/api.py
- v1 `Settings`/LMDB: https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/_internal/setting.py · https://cocoindex.io/docs/advanced_topics/internal_storage/
- v1 `App`/lifecycle: https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/_internal/app.py · https://cocoindex.io/docs/programming_guide/app/ · https://cocoindex.io/docs/programming_guide/sdk_overview/
- v1 `@coco.fn`/embedders: https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/_internal/function.py · https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/ops/sentence_transformers.py
- v1 Postgres connector (no CDC): https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/connectors/postgres/_source.py · https://cocoindex.io/docs/connectors/postgres
- v1 Qdrant connector: https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/python/cocoindex/connectors/qdrant/_target.py · example https://github.com/cocoindex-io/cocoindex/blob/v1.0.10/examples/text_embedding_qdrant/main.py
- v1 live mode / progress: https://cocoindex.io/docs/programming_guide/live_mode/ · https://cocoindex.io/docs/advanced_topics/progress_monitoring/
- v0→v1 migration table: https://github.com/cocoindex-io/cocoindex/blob/main/skills/cocoindex/SKILL.md
- v0 docs (settings/Postgres/live): https://cocoindex.io/docs-v0/core/settings/ · https://cocoindex.io/docs-v0/sources/postgres/ · https://cocoindex.io/docs-v0/core/flow_methods/
- PyPI version timeline: https://pypi.org/project/cocoindex/#history · https://pypi.org/pypi/cocoindex/json
