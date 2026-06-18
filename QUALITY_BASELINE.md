# Quality Baseline

## 2026-06-18 Gate Refresh

RAT-AUDIT-015 refreshed the local quality gates so the required Makefile targets match the GitHub Actions contract before broader test-suite debt is tackled. The historical 2026-05-18 snapshot below remains as provenance for older baseline debt; this section records the current target semantics measured from `/Users/po4yka/GitRep/ratatoskr-repositories/ratatoskr` after the gate cleanup.

| target | command | exit_code | current result | notes |
| --- | --- | --- | --- | --- |
| `make type` | `uv run --frozen mypy app --show-error-codes --pretty --cache-dir .mypy_cache` | 0 | `Success: no issues found in 962 source files` | Mirrors `.github/workflows/ci.yml` type-check job. |
| `make type-all` | `uv run --frozen mypy app tests` | 0 | `Success: no issues found in 1643 source files` | Test-suite typing debt has been fixed without a broad `tests.*` mypy suppression; remaining mypy output is informational notes for unchecked untyped function bodies. |
| `make security-bandit` | `uv run --frozen bandit -r app -ll` | 0 | No issues identified at the configured threshold | Split out so application security lint can be rerun independently from dependency-audit network/resolver behavior. |
| `make security-deps` | `bash tools/scripts/audit-deps.sh` | 0 | No known vulnerabilities found across 435 filtered PyPI packages | Uses the same requirement filters as the CI `pip-audit-scan` job and audits the compiled pinned set with `--no-deps --disable-pip` to avoid local temporary resolver-venv failures. |

Snapshot of the ratatoskr quality signal as measured on `main @ f05c8999`,
captured 2026-05-18 from the host venv after `uv sync --all-extras --dev`.
All commands were executed in `/Users/npochaev/GitHub/ratatoskr` with
`.venv/bin` first on `PATH` (Python 3.13.5, ruff 0.15.12). No files under
`app/`, `tests/`, or `pyproject.toml` were modified to produce this baseline.

`pytest-cov` was installed ad-hoc into the host venv (`.venv/bin/pip install
pytest-cov`) to satisfy the `make test-all` invocation, since the locked dev
dependencies do not include it; this did not touch `pyproject.toml`. The
`make check-lock` invocation regenerates `uv.lock`, `requirements.txt`, and
`requirements-dev.txt`; those three files were snapshotted before the run and
restored afterwards so the working tree is unchanged.

| target | command | exit_code | numeric_value | notes |
| --- | --- | --- | --- | --- |
| `make lint` | `ruff check .` + `tools/scripts/check_file_size.py` | 0 | 0 | ruff: "All checks passed!"; file-size guard also passed. Host system ruff (0.13.x) cannot parse `pyproject.toml` due to unknown `ASYNC240` selector -- venv ruff 0.15.12 is required. |
| `make type` | `uv run --frozen mypy app tests` | 2 | 33 errors / 5 files | "Found 33 errors in 5 files (checked 1299 source files)". Errors concentrated in `app/infrastructure/vector/`, `app/infrastructure/embedding/gemini_embedding_service.py`, `app/agents/langgraph/graph.py`, and `tests/test_message_coalescer.py`. |
| `make test-unit` | `pytest tests/ -m "not slow and not integration" -v` | 2 | 3 229 passed / 40 failed / 24 errors / 496 skipped / 24 deselected (47 warnings) in 1 348.57 s | Initial collection on the host's mise Python 3.11 produced 18 import errors; switching to the venv (Python 3.13) cleared those, and the run completed with the failure counts above. Exit 2 is from pytest reporting failures, not infrastructure. |
| `make test-all` (line coverage % for `app/`) | `pytest tests/ -v --cov=app --cov-report=term-missing` | 2 | 3 246 passed / 47 failed / 24 errors / 496 skipped (60 warnings) in 1 356.27 s; **TOTAL coverage 57.51 %** (61 094 statements, 23 519 missed, 16 324 branches, 2 403 partial) | Required ad-hoc `.venv/bin/pip install pytest-cov` (coverage 7.14.0, pytest-cov 7.1.0); plugin is not in the locked dev set. The configured `Required test coverage of 15.0%` gate was satisfied. Exit 2 comes from the failure count, not the coverage gate. |
| `make security` (bandit) | `uv run --frozen bandit -r app -ll` | 0 (within make: rolled into target exit 2 below) | High 0 / Medium 0 / Low 55 | bandit reported "No issues identified" at `-ll` (the target threshold). 126 413 LOC scanned. By-confidence: Low 0 / Medium 15 / High 40. |
| `make security` (pip-audit) | `uv run --frozen pip-audit` | 1 | 1 vulnerability / 1 package | `langchain-core 1.3.2` -- CVE-2026-44843 -- fixed in 0.3.85 or 1.3.3. This is what flips the overall `make security` target to exit 2. |
| `make static-checks` (semgrep mutability + bare-except) | `semgrep --config semgrep/python-mutability.yml --error app/ tests/` then `... python-bare-except.yml ...` | 0 | 0 findings (0 mutability + 0 bare-except) | Mutability ruleset: 6 rules on 817 files, 0 findings. Bare-except ruleset: 2 rules on 817 files, 0 findings. |
| `make check-openapi` | `pytest tests/api/test_openapi_sync.py -v` | 0 | 81 passed / 2 skipped | Includes JSON/YAML equivalence, wire-shape conventions, security declarations, route coverage. |
| `make check-lock` | `uv lock` + `uv export ...` + `git diff --exit-code uv.lock requirements.txt requirements-dev.txt` | 0 | 329 packages resolved, 0 diff lines | Lockfiles and exported requirement pins are in sync with `pyproject.toml`. |
| custom -- files in `app/` over 800 LOC | `find app -name '*.py' -exec wc -l {} + \| awk '$2 != "total" && $1 > 800 {print}'` | 0 | 4 files | `app/agents/multi_source_aggregation_agent.py` (958), `app/db/models/core.py` (850), `app/adapters/telegram/url_batch_processor.py` (821), `app/mcp/semantic_service.py` (815). |
| custom -- files in `app/` over 1200 LOC | same `find ... wc -l` pipeline with threshold 1200 | 0 | 0 files | Largest file (`multi_source_aggregation_agent.py`) is 958 LOC, well under the 1500-LOC `check_file_size.py` guard. |
| total LOC in `app/` (cloc) | `cloc --include-lang=Python app/` | 0 | 116 699 code / 21 202 blank / 12 080 comment / 800 files | `cloc` was installed via `brew install cloc` for this measurement. `wc -l` over the same tree reports 149 987 raw lines (includes blanks/comments). |

## Command excerpts

### `make lint`

```text
ruff check .
All checks passed!
python tools/scripts/check_file_size.py --max-loc 1500 --baseline tools/scripts/file_size_baseline.json
EXIT=0
```

### `make type`

```text
uv run --frozen mypy app tests
app/infrastructure/embedding/gemini_embedding_service.py:50: error: Module has no attribute "Client"  [attr-defined]
app/infrastructure/vector/summary_point.py:245: error: ...  [attr-defined]
...
app/agents/langgraph/graph.py:113: error: No overload variant of "ainvoke" of "Pregel" matches argument types "SummarizationGraphState", "dict[str, Any]"  [call-overload]
Found 33 errors in 5 files (checked 1299 source files)
EXIT=2
```

### `make test-unit` (final)

```text
pytest tests/ -m "not slow and not integration" -v
============================= test session starts ==============================
platform darwin -- Python 3.13.5, pytest-...
collecting ... collected 3188 items / 8 deselected / 7 skipped / 3180 selected
...
= 40 failed, 3229 passed, 496 skipped, 24 deselected, 47 warnings, 24 errors in 1348.57s (0:22:28) =
make: *** [test-unit] Error 1
EXIT=2
```

### `make test-all` (final)

```text
pytest tests/ -v --cov=app --cov-report=term-missing
============================= test session starts ==============================
...
TOTAL                                                                             61094  23519  16324   2403  57.51%
Required test coverage of 15.0% reached. Total coverage: 57.51%
= 47 failed, 3246 passed, 496 skipped, 60 warnings, 24 errors in 1356.27s (0:22:36) =
make: *** [test-all] Error 1
EXIT=2
```

### `make security` -- bandit

```text
uv run --frozen bandit -r app -ll
Run started:2026-05-18 15:40:21.221865+00:00
Test results:
        No issues identified.
Code scanned:
        Total lines of code: 126413
Run metrics:
        Total issues (by severity):
                Undefined: 0
                Low: 55
                Medium: 0
                High: 0
        Total issues (by confidence):
                Undefined: 0
                Low: 0
                Medium: 15
                High: 40
```

### `make security` -- pip-audit

```text
uv run --frozen pip-audit
Found 1 known vulnerability in 1 package
Name           Version ID             Fix Versions
-------------- ------- -------------- ------------
langchain-core 1.3.2   CVE-2026-44843 0.3.85,1.3.3
make: *** [security] Error 1
EXIT=2
```

### `make static-checks`

```text
semgrep --config semgrep/python-mutability.yml --error app/ tests/
Scanning 853 files tracked by git with 6 Code rules:
Scanning 817 files with 6 python rules.
Ran 6 rules on 817 files: 0 findings.
semgrep --config semgrep/python-bare-except.yml --error app/ tests/
Scanning 853 files tracked by git with 2 Code rules:
Scanning 817 files with 2 python rules.
Ran 2 rules on 817 files: 0 findings.
EXIT=0
```

### `make check-openapi`

```text
pytest tests/api/test_openapi_sync.py -v
...
tests/api/test_openapi_sync.py::TestJsonYamlSync::test_json_info_matches_yaml PASSED [100%]
======================== 81 passed, 2 skipped in 2.19s =========================
EXIT=0
```

### `make check-lock`

```text
uv lock
Resolved 329 packages in 8ms
uv export --no-dev --format requirements-txt -p 3.13 -o requirements.txt
Resolved 329 packages in 6ms
uv export --only-group dev --no-hashes --format requirements-txt -p 3.13 -o requirements-dev.txt
Resolved 329 packages in 7ms
EXIT=0
```

### Files in `app/` over 800 LOC

```text
$ find app -name '*.py' -type f -exec wc -l {} + | awk '$2 != "total" && $1 > 800 {print $1, $2}' | sort -rn
958 app/agents/multi_source_aggregation_agent.py
850 app/db/models/core.py
821 app/adapters/telegram/url_batch_processor.py
815 app/mcp/semantic_service.py
$ find app -name '*.py' -type f -exec wc -l {} + | awk '$2 != "total" && $1 > 1200 {print $1, $2}' | wc -l
       0
```

### Total LOC in `app/` via cloc

```text
$ cloc --include-lang=Python app/
github.com/AlDanial/cloc v 2.08  T=2.74 s (292.4 files/s, 54811.3 lines/s)
-------------------------------------------------------------------------------
Language                     files          blank        comment           code
-------------------------------------------------------------------------------
Python                         800          21202          12080         116699
-------------------------------------------------------------------------------
SUM:                           800          21202          12080         116699
-------------------------------------------------------------------------------
```

## Notes

- Two rows are flagged in-progress at capture time: `make test-unit` and
  `make test-all`. Both were launched in the venv against Python 3.13 after
  installing `pytest-cov`. Re-run them with `PATH=.venv/bin:$PATH make
  test-unit` and `... make test-all` to extract the final pass/fail counts and
  line-coverage percentage; the streamed runs in `/tmp/qb/test-unit.out` and
  `/tmp/qb/test-all.out` will hold the terminal coverage summary.
- The first batch of measurements ran with the host's mise Python 3.11; that
  pytest lacked `pytest-cov` *and* failed at collection time (18 import errors
  because the host interpreter cannot resolve the Python 3.13-only deps).
  Always activate `.venv` (or prefix `PATH=.venv/bin:$PATH`) before invoking
  `make` targets that shell out to bare `pytest`.
- Bandit's overall step exited 0 (`bandit -r app -ll` -- the `-ll` threshold
  suppresses Low findings); pip-audit flips `make security` to exit 2 via the
  langchain-core advisory. Treat the two as separate signals.
