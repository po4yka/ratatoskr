from __future__ import annotations

from app.adapters.content.streaming.section_assembler import SummarySectionStreamAssembler


def test_section_assembler_emits_sections_in_required_order() -> None:
    assembler = SummarySectionStreamAssembler()

    chunks = [
        '{"summary_250":"Short summary",',
        '"tldr":"TLDR section",',
        '"key_ideas":["Idea 1","Idea 2"],',
        '"topic_tags":["ai","telegram"]}',
    ]

    emitted_sections: list[str] = []
    for chunk in chunks:
        snapshots = assembler.add_delta(chunk)
        emitted_sections.extend(snapshot.section for snapshot in snapshots)

    assert emitted_sections == ["summary_250", "tldr", "key_ideas", "topic_tags"]


def test_section_assembler_tolerates_partial_json() -> None:
    assembler = SummarySectionStreamAssembler()

    snapshots = assembler.add_delta('{"summary_250":"Partial summary')
    assert [snapshot.section for snapshot in snapshots] == ["summary_250"]

    snapshots = assembler.add_delta(' completed","tldr":"Short TLDR"}')
    assert [snapshot.section for snapshot in snapshots] == ["summary_250", "tldr"]


def test_section_assembler_preview_rendering() -> None:
    assembler = SummarySectionStreamAssembler()

    assembler.add_delta(
        '{"summary_250":"Short summary","tldr":"Fast TLDR","key_ideas":["One"],"topic_tags":["bot"]}'
    )

    preview = assembler.render_preview(finalizing=True)
    assert "Summary:" in preview
    assert "TL;DR:" in preview
    assert "Key ideas:" in preview
    assert "Tags:" in preview
    assert "Finalizing output" in preview
