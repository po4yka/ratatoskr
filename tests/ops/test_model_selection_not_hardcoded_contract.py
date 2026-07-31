"""Compose must not pick the model. CLAUDE.md rule 11: ratatoskr.yaml decides.

An ``environment:`` entry is not a fallback -- it wins. Compose interpolates
``${VAR:-default}`` against the project directory (``ops/docker/``), which is not
where the repo's ``.env`` lives, and an ``environment:`` value overrides whatever
``env_file:`` supplied. So ``OLLAMA_MODEL=${OLLAMA_MODEL:-llama3.3}`` stamped
llama3.3 into all three services and silently beat both ratatoskr.yaml and the
operator's own .env. Verified against the real file before removal: all three
resolved to 'llama3.3' with nothing set.

Omitting the entry is what makes the documented precedence true. The variable
still reaches the container through ``env_file: ../../.env`` when an operator
sets it there, and with nothing set the factory raises "OLLAMA_MODEL is required
when the selected LLM provider uses it" at startup -- loud, and only when that
provider is actually selected.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Model selection only. base_url, api_key and timeouts are deployment config and
# may legitimately carry a compose default.
_MODEL_VARS = (
    "OPENROUTER_MODEL",
    "OPENROUTER_FALLBACK_MODELS",
    "OPENROUTER_FLASH_MODEL",
    "OPENROUTER_FLASH_FALLBACK_MODELS",
    "OPENROUTER_LONG_CONTEXT_MODEL",
    "OPENAI_MODEL",
    "ANTHROPIC_MODEL",
    "OLLAMA_MODEL",
    "ATTACHMENT_VISION_MODEL",
    "ATTACHMENT_VISION_FALLBACK_MODELS",
)

_COMPOSE_FILES = tuple(sorted((ROOT / "ops/docker").glob("docker-compose*.yml")))


def _assignments(text: str, var: str) -> list[str]:
    """Every ``- VAR=...`` line for *var*, ignoring comments."""
    pattern = re.compile(rf"^\s*-\s*{re.escape(var)}=(.*)$", re.MULTILINE)
    return [
        m.group(1).strip()
        for m in pattern.finditer(text)
        if not m.group(0).lstrip().startswith("#")
    ]


@pytest.mark.parametrize("compose_file", _COMPOSE_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("var", _MODEL_VARS)
def test_no_compose_file_hardcodes_a_model(compose_file: Path, var: str) -> None:
    text = compose_file.read_text(encoding="utf-8")
    for value in _assignments(text, var):
        # A bare passthrough or an explicitly empty default is harmless: it
        # forwards the operator's value and injects nothing when unset.
        assert value in (f"${{{var}}}", f"${{{var}:-}}"), (
            f"{compose_file.name} sets {var}={value}. An environment: entry "
            f"overrides both ratatoskr.yaml and env_file, so this silently "
            f"becomes the model. Drop the line; env_file already forwards it."
        )


def test_the_compose_files_were_actually_scanned() -> None:
    """A glob that matches nothing would make every assertion above vacuous."""
    assert _COMPOSE_FILES, "no docker-compose*.yml found under ops/docker"
    assert any(p.name == "docker-compose.yml" for p in _COMPOSE_FILES)


def test_the_detector_catches_the_pattern_that_was_removed() -> None:
    """Guards the regex itself against silently matching nothing."""
    sample = "    environment:\n      - OLLAMA_MODEL=${OLLAMA_MODEL:-llama3.3}\n"
    assert _assignments(sample, "OLLAMA_MODEL") == ["${OLLAMA_MODEL:-llama3.3}"]
    assert _assignments("      # - OLLAMA_MODEL=${OLLAMA_MODEL:-x}\n", "OLLAMA_MODEL") == []
