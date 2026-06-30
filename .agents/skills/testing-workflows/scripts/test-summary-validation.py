#!/usr/bin/env python3
"""Test summary JSON contract validation."""

from app.core.summary_contract import validate_and_shape_summary


def main() -> None:
    test_summary = {
        "summary_250": "Short summary here.",
        "summary_1000": "Longer summary with more details.",
        "tldr": "TLDR version",
        "key_ideas": ["idea1", "idea2", "idea3", "idea4", "idea5"],
        "topic_tags": ["#tech", "#ai"],
        "entities": {"people": [], "organizations": [], "locations": []},
        "estimated_reading_time_min": 5,
        "key_stats": [],
        "answered_questions": ["What is this?"],
        "readability": {"method": "Flesch-Kincaid", "score": 10.0, "level": "Grade 10"},
        "seo_keywords": ["keyword1", "keyword2"],
    }

    try:
        validated = validate_and_shape_summary(test_summary)
        print("Valid summary!")
        print(f"Keys: {list(validated.keys())}")
    except Exception as e:
        print(f"Validation failed: {e}")


if __name__ == "__main__":
    main()
