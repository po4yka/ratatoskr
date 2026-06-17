# Dependency Supply-Chain Reference

Ratatoskr's dependency resolution uses a private Safety CLI index in addition to PyPI and a CPU-only PyTorch extra index. This document explains the resolution topology, the failure modes that emerge when a subscription lapses, and the layered defenses that prevent a yanked or malicious release from entering the lock or the installed environment.

**Last Updated:** 2026-06-15

---

## Index topology

Three indexes participate in `uv lock` and `uv export`:

| Index | URL | Role |
|---|---|---|
| Safety CLI private index | `https://pkgs.safetycli.com/repository/po4yka/project/ratatoskr/pypi/simple/` | Serves curated, safety-scanned wheels; hosts clean `fastapi==0.136.1` |
| PyPI | `https://pypi.org/simple/` | Public default index |
| PyTorch CPU extra | `https://download.pytorch.org/whl/cpu` | CPU-only PyTorch wheels to avoid pulling CUDA binaries |

The Safety index is declared in `pyproject.toml` under `[[tool.uv.index]]` with `name = "safety"` and `default = false`, making it a supplemental index rather than a replacement for PyPI. Access requires a valid `SAFETY_API_KEY` (a Safety CLI subscription credential set as a GitHub Actions secret and as a local environment variable for maintainers running `uv lock` locally).

The `UV_EXTRA_INDEX_URL` workflow environment variable points at the PyTorch CPU index. The `UV_INDEX_STRATEGY: unsafe-best-match` strategy is set in both lock-regeneration workflows (see below), which instructs uv to prefer the best matching version across all indexes rather than stopping at the first satisfying match from the first index consulted.

---

## Resolution strategy and the widened surface

`unsafe-best-match` widens the resolution surface in two ways that matter for supply-chain risk:

1. **Multiple indexes are compared per package.** uv considers candidates from the Safety index, PyPI, and the PyTorch CPU index simultaneously and picks the best-matching version across all of them. This is intentional — it allows the Safety index to serve a curated wheel even when PyPI has a newer but unvetted version available.

2. **The PyTorch CPU extra index adds an additional resolution candidate source for every package, not just torch-related ones.** Any package name present on `download.pytorch.org/whl/cpu` is a candidate, which is a small but real widening of the attack surface beyond PyPI alone.

---

## The Safety index lapse failure mode

If the Safety CLI subscription lapses or `SAFETY_API_KEY` is absent, uv silently falls back to PyPI for packages that would otherwise have been served by the Safety index. For `fastapi`, this means:

1. The Safety index serves the known-clean `fastapi==0.136.1`. Without it, uv resolves `fastapi` from PyPI only.
2. PyPI's yank mechanism is advisory: a yanked release is not installed by a fresh `pip install fastapi` but **is** accepted when a version specifier explicitly matches it (`==0.136.3`). More critically, `uv lock` with a lax specifier (`>=0.128.0,<1`) will skip yanked versions — but only if uv respects the yank flag from that index. If a future malicious release is published to PyPI before it is yanked, a lock regeneration during the lapse window could resolve and commit it.
3. The net result is that a maintenance window where `SAFETY_API_KEY` is unset or expired during lock regeneration creates a window during which a yanked or newly-malicious release could be committed to `uv.lock` and subsequently exported to `requirements.txt`.

This is not a theoretical concern: `fastapi==0.136.3` (osv.dev/MAL-2026-4750, published 2026-05-23) was a confirmed malicious release that injected a `fastar` typosquat dependency via the `[standard]` extras group. PyPI has since yanked it, but the exclusion in `pyproject.toml` and the runtime guard exist as defense in depth against a recurrence.

---

## Layered defenses

### 1. `!=` exclusions in `pyproject.toml`

Known malicious or yanked releases are excluded at the resolver level with PEP 508 `!=` clauses on the relevant dependency specifier. The current exclusion set:

| Package | Excluded version | Advisory |
|---|---|---|
| `fastapi` | `0.136.3` | osv.dev/MAL-2026-4750 |

These clauses are enforced by uv at resolution time: no matter which index is consulted, uv will never resolve to an excluded version. This is the primary defense.

### 2. `check_excluded_versions.py` requirements-file guard

`tools/scripts/check_excluded_versions.py` reads the `!=` exclusions directly from `pyproject.toml` (so the exclusion list never drifts) and fails if any committed `requirements*.txt` file pins an excluded `name==version`. This closes the gap where a stale committed export artifact could pin an excluded release and be installed directly via `pip install -r` without going through uv's resolver. CI runs this script as part of the lockfile-freshness job; the guard exits 0 (clean) as of 2026-05-29.

### 3. Safety index reachability guard in lock-regeneration workflows

Both `update-locks.yml` (triggered on `pyproject.toml` push) and `regenerate-lockfiles.yml` (manual `workflow_dispatch`) include a pre-lock step that asserts `SAFETY_API_KEY` is set and that the Safety index URL returns HTTP 200 before `uv lock` runs. If either check fails the job exits immediately with a descriptive error rather than silently falling back to a PyPI-only resolution. See the "Safety index guard" step in each workflow.

### 4. Lockfile-freshness CI job

The `ci.yml` lockfile-freshness job (`check-lock`) verifies that committed `requirements*.txt` files match the current `uv.lock` state. A silent fallback that produced a different resolution would cause this check to fail on the next push, providing a backstop even if the reachability guard is somehow bypassed.

---

## The `graph` optional extra (LangGraph supply chain)

ADR-0001 was reversed (2026-06-15): LangGraph + langchain-core are re-adopted to orchestrate the summarize pipeline as a checkpointed state graph (see `docs/decisions/0001-no-langgraph.md`, `0004`, `0015`). The dependencies live behind an optional `graph` extra in `pyproject.toml`, so the **default image is unaffected** — it does not install the extra (verify: the default Dockerfile extra set `ml youtube export scheduler mcp api browser_scraper attachment otel` resolves with neither `langgraph-checkpoint-postgres` nor `psycopg-pool`).

The extra declares five direct dependencies — `langgraph>=1.2.4,<2`, `langchain-core>=1.4.0,<2`, `langgraph-checkpoint-postgres>=3,<4`, `psycopg[binary]>=3.3.4`, `psycopg-pool>=3.2`. The `>=3` floor on `langgraph-checkpoint-postgres` is load-bearing: the 3.x line declares `langgraph-checkpoint<5,>=4.1.0` and resolves it to 4.1.0 — the same version langgraph 1.x pins — whereas a `<3` cap selects a 2.x release that conflicts with `langgraph-checkpoint>=4.1.0` and fails to resolve. `langchain-openai` and `langchain-qdrant` are deliberately excluded (structured output stays on `instructor` per ADR-0006; retrieval reuses our own Qdrant client).

### Transitive closure of the `graph` extra (19 over the zero-extra base; only 2 net-new to the lock)

The table below is `uv export --extra graph` minus the zero-extra base export, so it lists the extra's full closure. **Only two of these are net-new `uv.lock` nodes** — the rest were already locked transitively before this change (the langchain/langgraph ecosystem via `scrapegraphai`/`scraper_ai`, `psycopg[binary]` already in the base, `websockets` via `uvicorn`).

| Package | Version | Role |
|---|---|---|
| `langgraph` | 1.2.4 | State-graph runtime |
| `langgraph-checkpoint` | 4.1.0 | Checkpoint base (transitive) |
| `langgraph-checkpoint-postgres` | 3.1.0 | Postgres checkpointer (`AsyncPostgresSaver`) |
| `langgraph-prebuilt` | 1.1.0 | Prebuilt graph helpers (transitive) |
| `langgraph-sdk` | 0.4.2 | SDK types (transitive) |
| `langchain-core` | 1.4.0 | Core message/runnable types |
| `langchain-protocol` | 0.0.15 | Protocol shims (transitive) |
| `langsmith` | 0.8.0 | Tracing client (transitive; unused — we use OTel) |
| `psycopg` / `psycopg-binary` | 3.3.4 | psycopg3 driver for the checkpointer (ADR-0004) |
| `psycopg-pool` | 3.3.1 | psycopg3 connection pool |
| `jsonpatch` 1.33, `jsonpointer` 3.1.1, `ormsgpack` 1.12.2, `requests-toolbelt` 1.0.0, `uuid-utils` 0.14.1, `websockets` 15.0.1, `xxhash` 3.7.0, `zstandard` 0.25.0 | — | Supporting transitive deps |

**Net-new to `uv.lock`:** only `langgraph-checkpoint-postgres` and `psycopg-pool`. The still-banned `langchain`, `langchain-community`, and `langchain-openai` are present only as transitive deps of `scrapegraphai` — never imported directly (enforced by the ruff banned-api guard). The `graph` extra therefore mostly *promotes* already-locked packages to direct dependencies.

### Export and scanning policy

- The `graph` extra is **not** included in the committed `requirements-all.txt` (the test-image export) — no runtime code imports langgraph yet, so the test image stays graph-free until a later track adds importing code and tests. `uv.lock` still resolves the extra (the two new nodes appear), so the lockfile-drift gate stays green with no change to the `make lock-uv` / `ci.yml` export commands.
- The `graph` extra **is** added to the comprehensive `requirements-safety.txt` export in `ci.yml` so Safety / pip-audit / OSV scan its full surface. langchain/langgraph advisories were already in scope via `scraper_ai`; this also covers `langgraph-checkpoint-postgres` and `psycopg-pool`. When a `langchain*` advisory is disclosed, triage it the same way as any other index-protected package and add a `!=` clause if a release must be excluded.

---

## Operator checklist: maintaining the Safety subscription

- Renew the Safety CLI subscription before expiry. An expired key causes the private index to return 401/403, which the reachability guard in both workflows will catch and surface as a job failure rather than a silent fallback.
- Rotate `SAFETY_API_KEY` in GitHub Actions secrets (`Settings → Secrets → Actions`) and in any local `.env` used by maintainers who run `uv lock` locally.
- When a new malicious or yanked FastAPI (or other index-protected) release is disclosed, add a `!=<version>` clause to the relevant dependency specifier in `pyproject.toml` before regenerating locks. Do not remove existing `!=` clauses even after PyPI yanks a release — the yank is advisory only and the exclusion is free.
- After adding a `!=` clause, regenerate locks via `make lock-uv` (or trigger the `regenerate-lockfiles` workflow) and confirm `check_excluded_versions.py` exits 0.

---

## Finding missing exclusions

Run `python tools/scripts/check_excluded_versions.py` from the repo root (with the venv active) to confirm no committed requirements file pins an excluded version. Cross-reference osv.dev and the Safety CLI advisory feed for newly-disclosed FastAPI (or other index-protected package) malicious releases that may not yet have a matching `!=` clause in `pyproject.toml`. Any such gap should be reported to the maintainer for addition to `pyproject.toml` before the next lock regeneration.
