"""Unit tests for SummarySectionStreamAssembler — new cases not covered by
tests/test_summary_section_stream_assembler.py.

Existing coverage (do NOT duplicate):
- Sections emitted in required order
- Tolerates partial JSON (split at string boundary)
- render_preview output

New cases added here:
- JSON split mid-token across multiple chunks merges correctly
- Malformed JSON is tolerated without raising
- Each section is emitted exactly once even when partial JSON repeats
"""

from __future__ import annotations

from app.adapters.content.streaming.section_assembler import SummarySectionStreamAssembler


def test_json_split_mid_token_merges_correctly() -> None:
    """A streamed JSON sequence split mid-token across chunks merges correctly."""
    assembler = SummarySectionStreamAssembler()

    # Deliver the JSON in three irregular chunks that split mid-value.
    chunk1 = '{"summary_250": "Hel'
    chunk2 = 'lo world", "tldr": "T'
    chunk3 = 'LDR"}'

    snapshots1 = assembler.add_delta(chunk1)
    snapshots2 = assembler.add_delta(chunk2)
    snapshots3 = assembler.add_delta(chunk3)

    all_sections = {s.section: s.value for s in snapshots1 + snapshots2 + snapshots3}

    assert "summary_250" in all_sections
    assert all_sections["summary_250"] == "Hello world"

    assert "tldr" in all_sections
    assert all_sections["tldr"] == "TLDR"


def test_malformed_json_is_tolerated_without_raising() -> None:
    """Malformed JSON deltas do not raise; assembler continues gracefully."""
    assembler = SummarySectionStreamAssembler()

    # Feed garbage that is not valid JSON at all.
    try:
        result = assembler.add_delta("}{bad json ][")
    except Exception as exc:  # pragma: no cover
        raise AssertionError(f"add_delta raised unexpectedly: {exc}") from exc

    # No crash — result may be empty or contain partial matches, either is fine.
    assert isinstance(result, list)

    # Assembler recovers: feeding valid JSON after garbage still works.
    result2 = assembler.add_delta('{"summary_250": "Recovered", "tldr": "OK"}')
    sections = {s.section: s.value for s in result2}
    assert sections.get("summary_250") == "Recovered"
    assert sections.get("tldr") == "OK"


def test_each_section_emitted_exactly_once_on_repeated_partial_json() -> None:
    """A section is emitted exactly once even when the same partial JSON appears in multiple deltas."""
    assembler = SummarySectionStreamAssembler()

    # First delta: complete JSON with summary_250 and tldr.
    first_payload = '{"summary_250": "First summary", "tldr": "First TLDR"}'
    snapshots1 = assembler.add_delta(first_payload)

    sections1 = [s.section for s in snapshots1]
    assert "summary_250" in sections1
    assert "tldr" in sections1

    # Second delta: identical JSON string (simulates duplicate streaming delta).
    snapshots2 = assembler.add_delta(first_payload)

    # The assembler must not re-emit sections whose value hasn't changed.
    sections2 = [s.section for s in snapshots2]
    assert "summary_250" not in sections2
    assert "tldr" not in sections2


def test_empty_delta_returns_empty_list() -> None:
    """An empty delta does not emit any sections."""
    assembler = SummarySectionStreamAssembler()
    assert assembler.add_delta("") == []


def test_new_value_for_same_section_is_re_emitted() -> None:
    """If a section's value changes across deltas it IS re-emitted."""
    assembler = SummarySectionStreamAssembler()

    assembler.add_delta('{"summary_250": "Draft"}')
    snapshots = assembler.add_delta('{"summary_250": "Final version with more content"}')
    sections = {s.section: s.value for s in snapshots}

    assert "summary_250" in sections
    assert sections["summary_250"] == "Final version with more content"


def test_plain_token_deltas_do_not_rescan_the_growing_payload(monkeypatch) -> None:
    assembler = SummarySectionStreamAssembler()
    original = assembler._extract_sections
    scanned_lengths: list[int] = []

    def recording_extract(raw_text: str):
        scanned_lengths.append(len(raw_text))
        return original(raw_text)

    monkeypatch.setattr(assembler, "_extract_sections", recording_extract)

    assembler.add_delta('{"summary_250":"')
    for _ in range(10_000):
        assert assembler.add_delta("x") == []
    snapshots = assembler.add_delta('"}')

    assert len(scanned_lengths) == 2
    assert snapshots[0].section == "summary_250"
    assert snapshots[0].value == "x" * 10_000
