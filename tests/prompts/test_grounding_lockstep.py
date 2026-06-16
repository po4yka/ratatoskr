"""T6: en+ru lockstep -- the anti-contamination delimiter is in all 4 prompts."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.application.graphs.summarize.nodes.ground import GROUNDING_BLOCK_HEADER

_PROMPTS = Path(__file__).parents[2] / "app" / "prompts"
_FILES = [
    "summary_system_en.txt",
    "summary_system_ru.txt",
    "summary_system_en_instructor.txt",
    "summary_system_ru_instructor.txt",
]
# The literal marker the model is told to treat as reference-only; it must be the
# SAME phrase the ground node wraps its hits in, in every language (lockstep).
_DELIMITER = "RELATED PRIOR SUMMARIES (reference only)"


@pytest.mark.parametrize("name", _FILES)
def test_grounding_delimiter_present_in_every_prompt(name: str) -> None:
    text = (_PROMPTS / name).read_text(encoding="utf-8")
    assert _DELIMITER in text, f"{name} is missing the grounding delimiter (en+ru lockstep)"
    lowered = text.lower()
    # The anti-contamination guard, in either language (en uses "do not
    # summarize", ru uses "не резюмируйте").
    assert "do not summarize" in lowered or "не резюмируйте" in lowered


def test_ground_block_header_matches_prompt_delimiter() -> None:
    # The dynamic block's header must contain the phrase the prompts reference,
    # or the model's reference-only instruction would never bind to the block.
    assert _DELIMITER in GROUNDING_BLOCK_HEADER
