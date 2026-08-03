"""LLMAttemptTrigger's docstring is the only description of what each value means.

It is what an operator reads before writing a query against ``llm_calls``, and it
had drifted badly: ``auto_backfill`` was documented as having no writer while the
legacy cascade path wrote it on every fallback, ``graph_node`` was marked RESERVED
behind a feature flag that no longer exists while the summarize graph wrote it
from four places, and ``repair_loop`` cited a module that had moved packages.

Someone querying by trigger would have filtered out most of the table and drawn
the wrong conclusion, which is worse than no documentation at all.

So this derives rather than restates: the docstring's own "reserved" markers are
parsed out and checked against the writers actually present in the tree. Both
directions matter -- a value that gains a writer must lose its reserved marker,
and a value that loses its last writer must gain one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.db.models.core import LLMAttemptTrigger

ROOT = Path(__file__).resolve().parents[2]

# A write, not a read: `attempt_trigger == "x"` must not count.
_WRITE_PATTERNS = (
    '"attempt_trigger": "{v}"',
    'attempt_trigger="{v}"',
    'initial_attempt_trigger="{v}"',
)

# The docstring calls a value reserved with either spelling. This is a marker
# word, matched anywhere in the bullet, so a live value's bullet must not use it
# even in a negation ("live, not reserved" reads as reserved here). Say what the
# value does instead.
_RESERVED_MARKERS = ("reserved", "RESERVED")


def _docstring() -> str:
    doc = LLMAttemptTrigger.__doc__
    assert doc, "the enum lost its docstring -- that is the whole contract"
    return doc


# Bullets arrive dedented, so match the marker rather than a fixed indent. An
# indent-specific pattern silently matched nothing and swallowed the whole
# docstring into every bullet, which made every value look reserved.
_BULLET = re.compile(r"\n\s*- ``")


def _documented_as_reserved(value: str) -> bool:
    """Whether the docstring's bullet for *value* calls it reserved."""
    doc = _docstring()
    start = doc.index(f"``{value}``")
    remainder = doc[start + len(value) + 4 :]
    nxt = _BULLET.search(remainder)
    bullet = remainder if nxt is None else remainder[: nxt.start()]
    return any(marker in bullet for marker in _RESERVED_MARKERS)


def _source_files() -> list[Path]:
    return [
        p
        for p in (ROOT / "app").rglob("*.py")
        # The model file declares the values; alembic pins the historical enum.
        if "alembic" not in p.parts and p != ROOT / "app/db/models/core.py"
    ]


def _writers(value: str) -> list[str]:
    needles = [pattern.format(v=value) for pattern in _WRITE_PATTERNS]
    found: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                found.append(str(path.relative_to(ROOT)))
                break
    return found


_VALUES = [member.value for member in LLMAttemptTrigger]


def test_every_value_is_documented() -> None:
    doc = _docstring()
    for value in _VALUES:
        assert f"``{value}``" in doc, f"{value} is in the enum and not in the docstring"


@pytest.mark.parametrize("value", _VALUES)
def test_reserved_means_no_writer(value: str) -> None:
    if not _documented_as_reserved(value):
        pytest.skip(f"{value} is not documented as reserved")
    writers = _writers(value)
    assert not writers, (
        f"{value} is documented as reserved but written by {writers}. "
        f"A query that skips it would miss those rows."
    )


@pytest.mark.parametrize("value", _VALUES)
def test_a_live_value_has_a_writer(value: str) -> None:
    if _documented_as_reserved(value):
        pytest.skip(f"{value} is documented as reserved")
    assert _writers(value), (
        f"{value} is documented as live and nothing writes it. "
        f"Either mark it reserved or delete it."
    )


def test_the_writer_detector_distinguishes_reads_from_writes() -> None:
    """metrics_llm.py compares against auto_backfill; that must not count."""
    comparison = 'if attempt_trigger == "auto_backfill":'
    assert not any(p.format(v="auto_backfill") in comparison for p in _WRITE_PATTERNS)
    assert any(
        p.format(v="auto_backfill") in '{"attempt_trigger": "auto_backfill"}'
        for p in _WRITE_PATTERNS
    )


def test_every_cited_module_exists() -> None:
    """A docstring that points at a moved module sends the reader nowhere."""
    for cited in re.findall(r"``(app/[\w/]+\.py)``", _docstring()):
        assert (ROOT / cited).is_file(), f"the docstring cites {cited}, which does not exist"
