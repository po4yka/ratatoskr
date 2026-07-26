# Dependency Supply-Chain Reference

Ratatoskr's dependency resolution uses a private Safety CLI index in addition to PyPI and a CPU-only PyTorch extra index. This document explains the resolution topology, the failure modes that emerge when a subscription lapses, and the layered defenses that prevent a yanked or malicious release from entering the lock or the installed environment.

**Last Updated:** 2026-07-18

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

## Base LangGraph runtime supply chain

ADR-0001 was reversed on 2026-06-15 and the graph subsequently became the sole summarize path (see ADR-0013/0015). Since 2026-07-18, LangGraph and its PostgreSQL checkpointer are base dependencies rather than an optional extra. A plain `uv sync`, the bot image, the API image, `requirements.txt`, and `requirements-all.txt` therefore all contain the real execution engine. This prevents a minimal developer install from silently collecting only mock/InMemory graph tests while production runs a different dependency surface.

The direct constraints in `pyproject.toml` and their current `uv.lock` resolutions are:

| Direct dependency | Constraint | Locked version | Role |
|---|---|---|---|
| `langgraph` | `>=1.2.6,<2` | 1.2.6 | State-graph runtime |
| `langchain-core` | `>=1.4.8,<2` | 1.4.8 | Callback/message primitives used by LangGraph streaming |
| `langgraph-checkpoint` | `>=4.1.1,<5` | 4.1.1 | Checkpoint base; 4.1.1 is the security floor for GHSA-fjqc-hq36-qh5p |
| `langgraph-checkpoint-postgres` | `>=3,<4` | 3.1.0 | `AsyncPostgresSaver` implementation |
| `psycopg[binary]` | `>=3.3.4` | 3.3.4 | PostgreSQL driver required by the saver |
| `psycopg-pool` | `>=3.2` | 3.3.1 | Dedicated checkpoint connection pool |

The `langgraph-checkpoint-postgres>=3` floor is load-bearing: the 3.x line is compatible with `langgraph-checkpoint` 4.x. Structured output remains on `instructor` and retrieval uses the existing Qdrant client, so `langchain-openai` and `langchain-qdrant` are not direct dependencies. Ruff continues to ban direct imports of the kitchen-sink `langchain`, `langchain-community`, and `langchain-openai` packages.

### Export and scanning policy

- `requirements.txt` is the base runtime export and includes LangGraph, `langgraph-checkpoint-postgres`, psycopg, and psycopg-pool.
- `requirements-all.txt` adds the API/ML/YouTube/export/scheduler/MCP extras on top of that same base and therefore includes the graph stack too. Both production Dockerfiles install from the base project plus their service extras; no `--extra graph` switch exists.
- The comprehensive security export in `ci.yml` starts from the same base and adds every optional feature extra, so Safety, pip-audit, and OSV always scan the graph stack. When a LangGraph/LangChain advisory is disclosed, triage it like any other base-runtime advisory and add a resolver exclusion or raise a direct floor when required.

---

## Operator checklist: maintaining the Safety subscription

- Renew the Safety CLI subscription before expiry. An expired key causes the private index to return 401/403, which the reachability guard in both workflows will catch and surface as a job failure rather than a silent fallback.
- Rotate `SAFETY_API_KEY` in GitHub Actions secrets (`Settings → Secrets → Actions`) and in any local `.env` used by maintainers who run `uv lock` locally.
- When a new malicious or yanked FastAPI (or other index-protected) release is disclosed, add a `!=<version>` clause to the relevant dependency specifier in `pyproject.toml` before regenerating locks. Do not remove existing `!=` clauses even after PyPI yanks a release — the yank is advisory only and the exclusion is free.
- After adding a `!=` clause, regenerate locks via `make lock-uv` (or trigger the `regenerate-lockfiles` workflow) and confirm `check_excluded_versions.py` exits 0.

---

## Finding missing exclusions

Run `python tools/scripts/check_excluded_versions.py` from the repo root (with the venv active) to confirm no committed requirements file pins an excluded version. Cross-reference osv.dev and the Safety CLI advisory feed for newly-disclosed FastAPI (or other index-protected package) malicious releases that may not yet have a matching `!=` clause in `pyproject.toml`. Any such gap should be reported to the maintainer for addition to `pyproject.toml` before the next lock regeneration.
