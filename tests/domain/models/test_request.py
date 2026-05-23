"""Unit tests for Request domain model."""

import pytest

from app.domain.models.request import Request, RequestStatus, RequestType


class TestRequest:
    """Test suite for Request domain model."""

    def test_create_request(self):
        """Test creating a request with valid data."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            input_url="https://example.com",
        )

        assert request.user_id == 123
        assert request.chat_id == 456
        assert request.request_type == RequestType.URL
        assert request.status == RequestStatus.PENDING
        assert request.input_url == "https://example.com"

    def test_mark_as_crawling(self):
        """Test marking request as crawling."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.PENDING,
        )

        request.mark_as_crawling()

        assert request.status == RequestStatus.CRAWLING

    def test_mark_as_crawling_from_invalid_state_raises_error(self):
        """Test that marking as crawling from invalid state raises error."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.COMPLETED,
        )

        with pytest.raises(ValueError, match="Cannot mark request as crawling"):
            request.mark_as_crawling()

    def test_mark_as_summarizing(self):
        """Test marking request as summarizing."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.CRAWLING,
        )

        request.mark_as_summarizing()

        assert request.status == RequestStatus.SUMMARIZING

    def test_mark_as_completed(self):
        """Test marking request as completed."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.SUMMARIZING,
        )

        request.mark_as_completed()

        assert request.status == RequestStatus.COMPLETED

    def test_mark_as_completed_from_completed_raises_error(self):
        """Test that completing already-completed request raises error."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.COMPLETED,
        )

        with pytest.raises(ValueError, match="Cannot mark request as completed"):
            request.mark_as_completed()

    def test_mark_as_error(self):
        """Test marking request as error."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.CRAWLING,
        )

        request.mark_as_error()

        assert request.status == RequestStatus.ERROR

    def test_mark_as_error_from_cancelled_raises_error(self):
        """Test that marking cancelled request as error raises error."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.CANCELLED,
        )

        with pytest.raises(ValueError, match="Cannot mark cancelled request as error"):
            request.mark_as_error()

    def test_mark_as_cancelled(self):
        """Test marking request as cancelled."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.PENDING,
        )

        request.mark_as_cancelled()

        assert request.status == RequestStatus.CANCELLED

    def test_mark_as_cancelled_when_completed_raises_error(self):
        """Test that cancelling completed request raises error."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.COMPLETED,
        )

        with pytest.raises(ValueError, match="Cannot cancel completed request"):
            request.mark_as_cancelled()

    def test_is_completed(self):
        """Test checking if request is completed."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.COMPLETED,
        )

        assert request.is_completed() is True

    def test_is_pending(self):
        """Test checking if request is pending."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.PENDING,
        )

        assert request.is_pending() is True

    def test_is_processing(self):
        """Test checking if request is processing."""
        request_crawling = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.CRAWLING,
        )
        request_summarizing = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.SUMMARIZING,
        )

        assert request_crawling.is_processing() is True
        assert request_summarizing.is_processing() is True

    def test_is_failed(self):
        """Test checking if request failed."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            status=RequestStatus.ERROR,
        )

        assert request.is_failed() is True

    def test_has_url(self):
        """Test checking if request has URL."""
        request_with_url = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            input_url="https://example.com",
        )
        request_without_url = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.FORWARD,
        )

        assert request_with_url.has_url() is True
        assert request_without_url.has_url() is False

    def test_get_url(self):
        """Test getting URL from request."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
            input_url="https://example.com",
            normalized_url="https://example.com/normalized",
        )

        # Should return normalized URL if available
        assert request.get_url() == "https://example.com/normalized"

    def test_set_language(self):
        """Test setting detected language."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
        )

        request.set_language("en")

        assert request.lang_detected == "en"

    def test_set_language_with_empty_string_raises_error(self):
        """Test that setting empty language raises error."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
        )

        with pytest.raises(ValueError, match="Language cannot be empty"):
            request.set_language("")

    def test_set_correlation_id(self):
        """Test setting correlation ID."""
        request = Request(
            user_id=123,
            chat_id=456,
            request_type=RequestType.URL,
        )

        request.set_correlation_id("abc-123")

        assert request.correlation_id == "abc-123"

    def test_str_representation(self):
        """Test string representation."""
        request = Request(
            id=123,
            user_id=456,
            chat_id=789,
            request_type=RequestType.URL,
            status=RequestStatus.PENDING,
        )

        str_repr = str(request)
        assert "Request(id=123" in str_repr
        assert "type=url" in str_repr
        assert "status=pending" in str_repr


class TestRequestStatusFieldtheoryImported:
    """Status value used by the fieldtheory bookmark ingestor."""

    def test_fieldtheory_imported_value(self):
        assert RequestStatus.FIELDTHEORY_IMPORTED.value == "fieldtheory_imported"
        assert RequestStatus("fieldtheory_imported") is RequestStatus.FIELDTHEORY_IMPORTED

    def test_request_constructible_with_fieldtheory_imported_status(self):
        request = Request(
            user_id=1,
            chat_id=2,
            request_type=RequestType.URL,
            status=RequestStatus.FIELDTHEORY_IMPORTED,
        )

        assert request.status == RequestStatus.FIELDTHEORY_IMPORTED
