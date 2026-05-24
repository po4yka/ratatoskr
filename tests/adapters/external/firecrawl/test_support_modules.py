import json

import pytest

from app.adapters.external.firecrawl.error_handler import ErrorHandler
from app.adapters.external.firecrawl.response_processor import ResponseProcessor
from app.adapters.external.firecrawl.result_builder import ResultBuilder
from app.adapters.external.firecrawl.validators import (
    validate_init,
    validate_scrape_inputs,
    validate_search_inputs,
)
from app.core.call_status import CallStatus


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _PayloadLogger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def __getattr__(self, name: str) -> object:
        def recorder(*args: object, **kwargs: object) -> None:
            self.calls.append((name, args, kwargs))

        return recorder


class _Options:
    def options_snapshot(self, *, mobile: bool, pdf: bool) -> dict[str, bool]:
        return {"mobile": mobile, "pdf": pdf}


def _valid_init_kwargs() -> dict[str, object]:
    return {
        "api_key": "fc-test",
        "timeout_sec": 30,
        "max_retries": 3,
        "backoff_base": 0.5,
        "max_connections": 10,
        "max_keepalive_connections": 5,
        "keepalive_expiry": 30.0,
        "credit_warning_threshold": 100,
        "credit_critical_threshold": 10,
        "max_response_size_mb": 50,
    }


def test_validate_init_accepts_valid_config_and_rejects_each_invalid_field() -> None:
    validate_init(**_valid_init_kwargs())  # type: ignore[arg-type]

    invalid_cases = [
        ("api_key", ""),
        ("api_key", "bad"),
        ("timeout_sec", 0),
        ("max_retries", 11),
        ("backoff_base", -1),
        ("max_connections", 101),
        ("max_keepalive_connections", 0),
        ("keepalive_expiry", 0.5),
        ("credit_warning_threshold", 0),
        ("credit_critical_threshold", 1001),
        ("max_response_size_mb", 0),
    ]

    for key, value in invalid_cases:
        kwargs = _valid_init_kwargs()
        kwargs[key] = value
        with pytest.raises(ValueError):
            validate_init(**kwargs)  # type: ignore[arg-type]


def test_validate_scrape_and_search_inputs() -> None:
    validate_scrape_inputs("https://example.test", None)
    assert validate_search_inputs("  topic  ", 3, 1) == "topic"

    with pytest.raises(ValueError, match="URL is required"):
        validate_scrape_inputs("", None)
    with pytest.raises(ValueError, match="URL too long"):
        validate_scrape_inputs("x" * 2049, None)
    with pytest.raises(ValueError, match="Invalid request_id"):
        validate_scrape_inputs("https://example.test", 0)
    with pytest.raises(ValueError, match="Search query is required"):
        validate_search_inputs("", 1, None)
    with pytest.raises(ValueError, match="Search query too long"):
        validate_search_inputs("x" * 501, 1, None)
    with pytest.raises(ValueError, match="Search limit must be an integer"):
        validate_search_inputs("topic", "1", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Search limit must be between"):
        validate_search_inputs("topic", 11, None)


def test_response_processor_detects_errors_and_extracts_content() -> None:
    processor = ResponseProcessor()

    assert processor.coerce_success(True) is True
    assert processor.coerce_success(None) is None
    assert processor.coerce_success("yes") is True
    assert processor.detect_error_in_body({"error": "boom"}) == (True, "boom")
    assert processor.detect_error_in_body({"success": False, "message": "bad"}) == (True, "bad")
    assert processor.detect_error_in_body({"data": []}) == (True, "No data returned in response")
    assert processor.detect_error_in_body({"data": [{"error": "one"}]}) == (True, "one")
    assert processor.detect_error_in_body({"data": {"error": "nested"}}) == (True, "nested")
    assert processor.detect_error_in_body({"markdown": "ok"}) == (False, None)

    markdown, html, metadata, links = processor.extract_content_fields(
        {"data": [{"markdown": "md", "metadata": {"a": 1}, "links": ["x"], "summary": "s"}]}
    )
    assert (markdown, html, links) == ("md", None, ["x"])
    assert metadata == {"a": 1, "summary_text": "s"}

    markdown, html, metadata, links = processor.extract_content_fields(
        {"data": {"html": "<p>x</p>", "images": ["shot"]}}
    )
    assert (markdown, html, links) == (None, "<p>x</p>", None)
    assert metadata == {"screenshots": ["shot"]}


def test_response_processor_extracts_error_content_and_enriches_metadata() -> None:
    processor = ResponseProcessor()

    assert processor.enrich_metadata({"summary": "s"}, {"title": "t"}) == {
        "title": "t",
        "summary_text": "s",
    }
    assert processor.extract_error_content(
        {"data": {"markdown": "partial", "metadata": {"m": 1}, "links": ["l"], "images": ["i"]}}
    ) == ("partial", None, {"m": 1}, ["l"], None, ["i"])


def test_error_handler_retry_and_error_mapping() -> None:
    handler = ErrorHandler(max_retries=2, backoff_base=0.5)

    assert handler.should_retry(500, 0)
    assert handler.should_retry(400, 0, "timed out")
    assert not handler.should_retry(400, 0)
    assert not handler.should_retry(500, 2)
    assert handler.handle_retryable_errors(
        resp=_Response(429),  # type: ignore[arg-type]
        data={"retry_after": 10},
        attempt=0,
        cur_mobile=False,
        cur_pdf=False,
        pdf_hint=False,
    ) == (0.5, False)
    assert handler.handle_retryable_errors(
        resp=_Response(500),  # type: ignore[arg-type]
        data={},
        attempt=1,
        cur_mobile=False,
        cur_pdf=False,
        pdf_hint=False,
    ) == (1.0, True)
    assert handler.handle_retryable_errors(
        resp=_Response(408),  # type: ignore[arg-type]
        data={"message": "timeout"},
        attempt=0,
        cur_mobile=False,
        cur_pdf=False,
        pdf_hint=False,
    ) == (0.5, False)
    assert handler.handle_retryable_errors(
        resp=_Response(400),  # type: ignore[arg-type]
        data={},
        attempt=0,
        cur_mobile=False,
        cur_pdf=False,
        pdf_hint=False,
    ) == (None, False)

    assert ErrorHandler.map_status_error(400, "bad") == "Bad Request: bad"
    assert ErrorHandler.map_status_error(401, "bad") == "Unauthorized: bad"
    assert ErrorHandler.map_status_error(402, "bad") == "Payment Required: bad"
    assert ErrorHandler.map_status_error(404, "bad") == "Not Found: bad"
    assert ErrorHandler.map_status_error(429, "bad") == "Rate Limit Exceeded: bad"
    assert ErrorHandler.map_status_error(503, "bad") == "Server Error: bad"
    assert ErrorHandler.map_status_error(418, "bad") == "bad"


def test_error_handler_builds_search_error_results() -> None:
    payload_logger = _PayloadLogger()
    handler = ErrorHandler(payload_logger=payload_logger)  # type: ignore[arg-type]
    started = 0.0

    size_result = handler.build_search_size_error(Exception("too big"), "query", started)  # type: ignore[arg-type]
    http_result = handler.build_search_http_error(RuntimeError("network"), "query", started)
    invalid_json_result = handler.build_search_invalid_json_error(
        json.JSONDecodeError("bad", "x", 0),
        _Response(200),  # type: ignore[arg-type]
        12,
    )

    assert size_result.status == CallStatus.ERROR.value
    assert size_result.error_text == "Response too large: too big"
    assert http_result.error_text == "network"
    assert invalid_json_result.http_status == 200
    assert {call[0] for call in payload_logger.calls} == {
        "log_search_size_error",
        "log_search_http_error",
        "log_search_invalid_json",
    }


def test_result_builder_builds_success_error_and_fallback_results() -> None:
    builder = ResultBuilder(_Options())  # type: ignore[arg-type]

    success = builder.build_success_result(
        data={"status_code": 200, "markdown": "md", "metadata": {"title": "T"}, "cid": "cid"},
        latency=5,
        url="https://example.test",
        options_snapshot={"mobile": False},
        request_id=1,
        cur_pdf=False,
    )
    assert success.status is CallStatus.OK
    assert success.content_markdown == "md"
    assert success.correlation_id == "cid"

    body_error = builder.build_success_result(
        data={
            "status_code": 200,
            "error": "body failed",
            "markdown": "partial",
            "metadata": {"title": "T"},
        },
        latency=6,
        url="https://example.test",
        options_snapshot={"mobile": False},
        request_id=1,
        cur_pdf=False,
    )
    assert body_error.status is CallStatus.ERROR
    assert body_error.content_markdown == "partial"

    simple_error = builder.build_error_result(
        500, 7, "failed", "https://example.test", {"mobile": True}
    )
    assert simple_error.http_status == 500
    assert simple_error.error_text == "failed"

    non_retryable = builder.build_non_retryable_error_result(
        data={"markdown": "partial", "metadata": {"title": "T"}, "cid": "cid", "success": False},
        http_status=400,
        latency=8,
        url="https://example.test",
        options_snapshot={"mobile": False},
        request_id=2,
        cur_pdf=False,
        error_message="bad",
    )
    assert non_retryable.response_success is False
    assert non_retryable.response_error_message == "bad"

    fallback = builder.build_fallback_result(
        last_error="failed",
        last_latency=9,
        last_data={"markdown": "ignored", "success": False, "code": "E", "error": "bad"},
        url="https://example.test",
        cur_mobile=True,
        pdf_hint=True,
    )
    assert fallback.options_json == {"mobile": True, "pdf": True}
    assert fallback.response_error_code == "E"
