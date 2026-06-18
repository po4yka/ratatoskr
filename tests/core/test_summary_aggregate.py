from __future__ import annotations

from typing import Any

from app.core.summary_aggregate import (
    _dedupe_list,
    _dedupe_sentences,
    _merge_entities,
    _merge_key_stats,
    _select_best_summary_250,
    aggregate_chunk_summaries,
)


def test_summary_aggregate_empty_payload_has_contract_shape() -> None:
    result = aggregate_chunk_summaries([])

    assert result["summary_250"] == ""
    assert result["summary_1000"] == ""
    assert result["tldr"] == ""
    assert result["key_ideas"] == []
    assert result["topic_tags"] == []
    assert result["entities"] == {"people": [], "organizations": [], "locations": []}
    assert result["estimated_reading_time_min"] == 0
    assert result["readability"]["level"] == "Unknown"
    assert result["insights"]["new_facts"] == []
    assert result["insights"]["caution"] is None


def test_summary_aggregate_dedupes_and_limits_lists() -> None:
    assert _dedupe_list([" A ", "a", "", "B", "b", "C"], limit=2) == ["A", "B"]
    assert _select_best_summary_250(["short", "the longest summary", "short"]) == (
        "the longest summary"
    )
    assert _dedupe_sentences(
        ["Alpha sentence is unique. Beta sentence is unique.", "alpha sentence is unique."]
    ) == ("Alpha sentence is unique. Beta sentence is unique.")

    assert _merge_entities(
        {"people": ["Ada"], "organizations": ["OpenAI"], "locations": ["Paris"]},
        {"people": ["ada", "Grace"], "organizations": ["OpenAI", "NASA"]},
    ) == {
        "people": ["Ada", "Grace"],
        "organizations": ["OpenAI", "NASA"],
        "locations": ["Paris"],
    }

    stats = _merge_key_stats(
        [
            {"label": "Revenue", "value": "12.5", "unit": "USD", "source_excerpt": "report"},
            {"label": "bad value", "value": object()},
        ],
        [
            {"label": "revenue", "value": 99},
            {"label": "Users", "value": 42},
            {"value": 1},
        ],
    )
    assert stats == [
        {
            "label": "Revenue",
            "value": 12.5,
            "unit": "USD",
            "source_excerpt": "report",
        },
        {"label": "Users", "value": 42.0, "unit": None, "source_excerpt": None},
    ]


def test_summary_aggregate_merges_chunk_payloads() -> None:
    chunks: list[dict[str, Any]] = [
        {
            "summary_250": "Short first summary.",
            "summary_1000": "Alpha sentence is unique. Beta sentence is unique.",
            "tldr": "Alpha sentence is unique. Gamma sentence is unique.",
            "key_ideas": ["Idea A", "idea a", "Idea B"],
            "topic_tags": ["AI", "ai", "Robotics"],
            "entities": {
                "people": ["Ada"],
                "organizations": ["OpenAI"],
                "locations": ["Paris"],
            },
            "estimated_reading_time_min": "3",
            "key_stats": [{"label": "Users", "value": 100, "unit": "people"}],
            "answered_questions": ["What happened?", "What happened?"],
            "seo_keywords": ["AI", "Robotics", "ai"],
            "insights": {
                "topic_overview": "Overview one",
                "new_facts": [
                    {
                        "fact": "Fact A",
                        "why_it_matters": "because",
                        "source_hint": "source",
                        "confidence": 0.9,
                    },
                    {"fact": "Fact A", "confidence": 0.1},
                    "bad",
                ],
                "open_questions": ["Question A", "question a"],
                "suggested_sources": ["Source A"],
                "expansion_topics": ["Topic A"],
                "next_exploration": ["Next A"],
                "caution": "Caution A",
            },
        },
        {
            "summary_250": "This second summary is much more informative than the first.",
            "summary_1000": "Beta sentence is unique. Delta sentence is unique.",
            "key_ideas": ["Idea C"],
            "topic_tags": ["Policy"],
            "entities": {
                "people": ["Ada", "Grace"],
                "organizations": ["NASA"],
                "locations": ["Paris", "London"],
            },
            "estimated_reading_time_min": 4,
            "key_stats": [{"label": "Users", "value": 200}],
            "answered_questions": ["Why now?"],
            "seo_keywords": ["Policy"],
            "insights": {
                "topic_overview": "Overview two",
                "new_facts": [{"fact": "Fact B"}],
                "open_questions": ["Question B"],
                "suggested_sources": ["Source B"],
                "expansion_topics": ["Topic B"],
                "next_exploration": ["Next B"],
                "caution": "Caution B",
            },
        },
        {
            "estimated_reading_time_min": object(),
            "key_stats": [{"label": "Broken", "value": object()}],
        },
    ]

    result = aggregate_chunk_summaries(chunks)

    assert result["summary_250"] == "This second summary is much more informative than the first."
    assert result["summary_1000"] == (
        "Alpha sentence is unique. Beta sentence is unique. Delta sentence is unique."
    )
    assert result["tldr"] == (
        "Alpha sentence is unique. Gamma sentence is unique. "
        "Beta sentence is unique. Delta sentence is unique."
    )
    assert result["key_ideas"] == ["Idea A", "Idea B", "Idea C"]
    assert result["topic_tags"] == ["AI", "Robotics", "Policy"]
    assert result["entities"] == {
        "people": ["Ada", "Grace"],
        "organizations": ["OpenAI", "NASA"],
        "locations": ["Paris", "London"],
    }
    assert result["estimated_reading_time_min"] == 7
    assert result["key_stats"] == [
        {"label": "Users", "value": 100.0, "unit": "people", "source_excerpt": None}
    ]
    assert result["answered_questions"] == ["What happened?", "Why now?"]
    assert result["seo_keywords"] == ["AI", "Robotics", "Policy"]
    assert result["insights"] == {
        "topic_overview": "Overview one\n\nOverview two",
        "new_facts": [
            {
                "fact": "Fact A",
                "why_it_matters": "because",
                "source_hint": "source",
                "confidence": 0.9,
            },
            {"fact": "Fact B", "why_it_matters": None, "source_hint": None, "confidence": None},
        ],
        "open_questions": ["Question A", "Question B"],
        "suggested_sources": ["Source A", "Source B"],
        "expansion_topics": ["Topic A", "Topic B"],
        "next_exploration": ["Next A", "Next B"],
        "caution": "Caution A\n\nCaution B",
    }
