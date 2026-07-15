# Python Mutability and Aliasing Guide

This document explains the aliasing hazards that are automatically detected in this codebase and how to write safe alternatives.

## The Problem

Python variables hold *references*, not value copies.  When you assign a mutable object (list, dict, set) to multiple names or slots, every name shares the same underlying object.  A mutation through any one of them is visible through all the others — this is *aliasing*.

## High-Risk Patterns

### 1. `[mutable] * N`

```python
# Bug: all three slots point to the same inner list
rows = [[]] * 3
rows[0].append(1)
print(rows)  # [[1], [1], [1]] — all three mutated!

# Fix: comprehension creates independent objects
rows = [[] for _ in range(3)]
rows[0].append(1)
print(rows)  # [[1], [], []]
```

The same applies to dicts and sets inside the outer list:

```python
# Bug
buckets = [{}] * 10
# Fix
buckets = [{} for _ in range(10)]
```

### 2. `dict.fromkeys(keys, mutable)`

```python
# Bug: all keys map to the same list object
d = dict.fromkeys(["a", "b", "c"], [])
d["a"].append(1)
print(d)  # {"a": [1], "b": [1], "c": [1]}

# Fix: comprehension gives each key its own list
d = {k: [] for k in ["a", "b", "c"]}
d["a"].append(1)
print(d)  # {"a": [1], "b": [], "c": []}
```

### 3. Mutable default arguments

```python
# Bug (Ruff B006): `items` is shared across all calls that omit the arg
def process(items=[]):
    items.append("x")
    return items

# Fix: use None as sentinel, initialise in the body
def process(items=None):
    if items is None:
        items = []
    items.append("x")
    return items
```

### 4. Late-binding closures in loops

```python
# Bug (Ruff B023): all handlers see the *final* value of `key`
handlers = []
for key in ["a", "b", "c"]:
    handlers.append(lambda: key)

print([h() for h in handlers])  # ["c", "c", "c"]

# Fix: bind the value at creation time via a default argument
handlers = []
for key in ["a", "b", "c"]:
    handlers.append(lambda k=key: k)

print([h() for h in handlers])  # ["a", "b", "c"]
```

### 5. Aliasing a caller's list in constructors

```python
# Bug: mutations to the original list after construction affect the object
class Chain:
    def __init__(self, providers):
        self._providers = providers  # alias!

providers = [a, b]
chain = Chain(providers)
providers.append(c)          # also appended to chain._providers!

# Fix: defensive copy in the constructor
class Chain:
    def __init__(self, providers):
        self._providers = list(providers)
```

### 6. Returning internal mutable state from properties

```python
# Bug: caller can mutate internal state
@property
def items(self):
    return self._items   # caller gets the real list

# Fix: return a copy
@property
def items(self):
    return list(self._items)
```

## Identity vs Value Comparison: `is` vs `==`

Python has two distinct equality operators:

| Operator | Checks | Use when |
|----------|--------|----------|
| `is` / `is not` | **Object identity** (same memory address) | Singletons: `None`, sentinels, deliberate identity |
| `==` / `!=` | **Value equality** | Everything else |

### The bug

```python
# Bug: "ok" may or may not be interned; this can silently return False
status = get_status_from_json()
if status is "ok":          # F632: use == instead
    ...

# Bug: integers outside [-5, 256] are not cached by CPython
code = http_response.status_code
if code is 200:             # F632: use == instead
    ...
```

### The fix

```python
# Correct value comparisons
if status == "ok":
    ...
if code == 200:
    ...
```

### What legitimately uses `is`

```python
# Singleton: None
if value is None:           # correct — None is always the same object
    ...
if value is not None:       # correct
    ...

# Private sentinel for "not provided" (distinct from None)
MISSING = object()
def f(x=MISSING):
    if x is MISSING:        # correct — MISSING is always the same object
        ...

# Ellipsis sentinel (stdlib pattern)
def g(callback=...):
    if callback is not ...: # correct — Ellipsis is a singleton
        ...

# Real Enum members (project convention)
if result.status is CallStatus.OK:  # OK — enum members are singletons
    ...
```

### None specifically: always use `is` / `is not`

`None` is a singleton; comparing it with `==` or `!=` is also wrong:

```python
# Bug (Ruff E711): can invoke custom __eq__, wrong for numpy arrays, etc.
if value == None:
    ...
if result != None:
    ...

# Fix: singleton identity check
if value is None:
    ...
if result is not None:
    ...
```

Ruff `E711` (comparison-to-None) enforces this via the `"E"` selector in `pyproject.toml`. It catches `== None`, `!= None`, `None == x`, and `None != x`.

### Rule

Ruff `F632` enforces the `is`/`is not`-vs-literal rule automatically. It fires on `is`/`is not` against any literal value (string, int, float, bytes, …) and is enabled project-wide via the `"F"` pyflakes selector in `pyproject.toml`. If you have a genuine false-positive (rare), suppress it narrowly:

```python
result = cache.get(key)
if result is _SENTINEL:  # noqa: F632 — _SENTINEL is an object() singleton, not a value literal
    ...
```

## Bare `except:` Clauses

A bare `except:` catches **`BaseException`** — the root of Python's entire exception hierarchy — including:

- `KeyboardInterrupt` (Ctrl-C / SIGINT): prevents graceful shutdown
- `SystemExit` (raised by `sys.exit()`): prevents clean process termination
- `asyncio.CancelledError` (Python 3.8+): swallows async task cancellation
- Memory errors, segfaults, and other fatal conditions

### The bug

```python
# Bug: catches KeyboardInterrupt and SystemExit too
try:
    result = dangerous_operation()
except:
    logger.error("Operation failed")  # silently swallows shutdown signals
```

### The fix

```python
# At a boundary — catch ordinary program errors only
try:
    result = dangerous_operation()
except Exception as exc:
    logger.exception("Operation failed: %s", exc)

# For a specific case — narrow to exactly what can go wrong
try:
    data = json.loads(raw)
except (ValueError, TypeError) as exc:
    logger.warning("Invalid JSON: %s", exc)
    return None
```

### Rules

- Use **`except Exception`** when you need a true catch-all for ordinary program errors at a boundary. Log the exception; never swallow silently.
- Use **specific exception types** (or a tuple) for expected failure modes.
- Never use bare `except:` — it prevents graceful shutdown and hides bugs.
- In async code, do not catch `asyncio.CancelledError`; if you must enter a broad handler in async code, re-raise cancellation:

  ```python
  import asyncio

  try:
      await operation()
  except asyncio.CancelledError:
      raise  # always let cancellation propagate
  except Exception as exc:
      logger.exception("Async operation failed: %s", exc)
  ```

Ruff `E722` (bare-except) enforces this automatically via the `"E"` selector. If a suppression is genuinely needed, use a narrow inline `# noqa: E722` with a comment explaining why the bare handler is safe.

## Automatic Detection

Three complementary layers enforce these rules:

| Layer | What it catches | When it runs |
|-------|----------------|--------------|
| **Ruff E711** | `== None` / `!= None` instead of `is` | `make lint`, pre-commit, CI |
| **Ruff E722** | Bare `except:` instead of `except Exception` | `make lint`, pre-commit, CI |
| **Ruff F632** | `is`/`is not` against value literals | `make lint`, pre-commit, CI |
| **Ruff B006** | Mutable default arguments | `make lint`, pre-commit, CI |
| **Ruff B023** | Late-binding closures in loops | `make lint`, pre-commit, CI |
| **Semgrep** (`semgrep/python-mutability.yml`) | `[mutable]*N`, `dict.fromkeys(keys, mutable)` | `make static-checks`, pre-push, CI |
| **Semgrep** (`semgrep/python-bare-except.yml`) | Bare `except:`, `except BaseException` without justification | `make static-checks`, pre-push, CI |
| **Architecture tests** (`tests/architecture/`) | All patterns above (regression fixtures) | `make test`, CI |

Run `make static-checks` locally before pushing to catch Semgrep findings early.

## Code Review Checklist

When reviewing Python code, look for:

- [ ] Any `[x] * N` where `x` is a list, dict, set, or call returning one
- [ ] Any `dict.fromkeys(keys, value)` where `value` is mutable
- [ ] Any function or method that stores a passed-in list/dict/set without copying it
- [ ] Any property or accessor that returns a reference to an internal collection
- [ ] Any lambda or nested `def` inside a loop that references the loop variable
- [ ] Any class with a mutable attribute defined at class scope (not in `__init__`)

## Safe Patterns at a Glance

| Instead of | Use |
|------------|-----|
| `[[]] * N` | `[[] for _ in range(N)]` |
| `[{}] * N` | `[{} for _ in range(N)]` |
| `dict.fromkeys(keys, [])` | `{k: [] for k in keys}` |
| `dict.fromkeys(keys, {})` | `{k: {} for k in keys}` |
| `def f(items=[])` | `def f(items=None)` then `if items is None: items = []` |
| `lambda: key` in a loop | `lambda k=key: k` |
| `self._x = passed_list` | `self._x = list(passed_list)` |
| `return self._x` (from property) | `return list(self._x)` |
