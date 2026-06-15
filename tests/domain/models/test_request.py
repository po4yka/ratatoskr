"""Unit tests for Request domain model."""

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


class TestRequestStatusXImported:
    """Status value used by the x_bookmarks bookmark ingestor."""

    def test_x_imported_value(self):
        assert RequestStatus.X_IMPORTED.value == "x_imported"
        assert RequestStatus("x_imported") is RequestStatus.X_IMPORTED

    def test_request_constructible_with_x_imported_status(self):
        request = Request(
            user_id=1,
            chat_id=2,
            request_type=RequestType.URL,
            status=RequestStatus.X_IMPORTED,
        )

        assert request.status == RequestStatus.X_IMPORTED
