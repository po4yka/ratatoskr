"""Process-wide cache for prompt text files.

Prompt files are static for the lifetime of a process (they only change on
deploy/restart), but several hot-path callers re-read them from disk on every
LLM invocation. Routing those reads through this module reads each file at most
once per process, removing blocking disk I/O from the request path.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=128)
def _read_text_cached(path_str: str) -> str:
    return Path(path_str).read_text(encoding="utf-8")


def read_prompt_text(path: Path | str, *, strip: bool = False) -> str:
    """Return the prompt file's text, reading from disk only on the first call.

    Args:
        path: Path to the prompt file.
        strip: When True, strip surrounding whitespace (cached read is unaffected).
    """
    text = _read_text_cached(str(path))
    return text.strip() if strip else text


def clear_prompt_file_cache() -> None:
    """Clear the cached prompt files. Intended for test teardown only."""
    _read_text_cached.cache_clear()
