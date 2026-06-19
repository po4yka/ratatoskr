from __future__ import annotations

import pytest

from app.cli import seed_demo_data

pytestmark = pytest.mark.no_network


def test_seed_demo_data_parser_defaults_to_dev_user_and_ten_items() -> None:
    args = seed_demo_data.build_parser().parse_args([])

    assert args.user_id == seed_demo_data.DEFAULT_DEV_USER_ID
    assert args.count == seed_demo_data.DEFAULT_DEMO_COUNT


def test_summary_payload_contains_library_view_fields() -> None:
    payload = seed_demo_data._summary_payload(seed_demo_data.DEMO_SUMMARIES[0])

    assert payload["title"]
    assert payload["summary_250"]
    assert payload["summary_1000"]
    assert payload["tldr"]
    assert payload["topic_tags"]
    assert payload["estimated_reading_time_min"] > 0
