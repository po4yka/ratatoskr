from app.adapters.external.firecrawl.parsing import (
    extract_error_message,
    extract_result_items,
    extract_total_results,
    has_url_field,
    normalize_search_item,
    normalize_text,
)


def test_normalize_text_handles_empty_scalars() -> None:
    assert normalize_text(None) is None
    assert normalize_text("  value  ") == "value"
    assert normalize_text(42) == "42"
    assert normalize_text("   ") is None


def test_extract_total_results_walks_nested_payloads_and_cycles() -> None:
    payload: dict[str, object] = {"data": [{"ignored": True}, {"total_results": 7}]}
    payload["self"] = payload

    assert extract_total_results(payload) == 7
    assert extract_total_results({"data": {"total": -1}}) is None


def test_extract_error_message_prefers_first_nested_message() -> None:
    assert extract_error_message({"data": [{"message": "  failed  "}]}) == "failed"
    assert extract_error_message([{"data": {"error": "boom"}}]) == "boom"
    assert extract_error_message({"data": [{"message": " "}]}) is None


def test_extract_result_items_finds_url_bearing_items() -> None:
    payload = {"data": {"matches": [{"title": "No URL"}, {"link": "https://example.test"}]}}

    assert has_url_field({"sourceUrl": " https://source.test "})
    assert extract_result_items(payload) == [{"link": "https://example.test"}]
    assert extract_result_items({"data": {"items": [{"name": "empty"}]}}) == []


def test_normalize_search_item_maps_aliases_and_cleans_snippet() -> None:
    item = normalize_search_item(
        {
            "permalink": " https://example.test/article ",
            "headline": " Headline ",
            "content": "Line one\n\nLine two",
            "source": [" Example ", None, " Blog "],
            "publishedAt": {"iso": "2026-01-01"},
        }
    )

    assert item is not None
    assert item.url == "https://example.test/article"
    assert item.title == "Headline"
    assert item.snippet == "Line one Line two"
    assert item.source == "Example, Blog"
    assert item.published_at == "2026-01-01"


def test_normalize_search_item_requires_url_and_defaults_title() -> None:
    assert normalize_search_item({"title": "missing url"}) is None

    item = normalize_search_item({"url": "https://example.test"})

    assert item is not None
    assert item.title == "https://example.test"
