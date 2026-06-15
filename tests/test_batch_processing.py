"""Tests for batch processing models and formatters."""

import time
import unittest

from app.adapter_models.batch_processing import (
    URLBatchStatus,
    URLStatus,
    URLStatusEntry,
)
from app.adapters.external.formatting.batch_progress_formatter import (
    MAX_MESSAGE_LENGTH,
    BatchProgressFormatter,
)


class TestURLStatusEntry(unittest.TestCase):
    """Test suite for URLStatusEntry dataclass."""

    def test_create_entry_extracts_domain(self):
        """Test that domain is automatically extracted from URL."""
        entry = URLStatusEntry(url="https://www.example.com/article/123")
        assert entry.domain == "example.com"

    def test_domain_extraction_removes_www(self):
        """Test that www. prefix is removed from domain."""
        entry = URLStatusEntry(url="https://www.techcrunch.com/news")
        assert entry.domain == "techcrunch.com"

    def test_domain_extraction_no_www(self):
        """Test domain extraction when URL has no www."""
        entry = URLStatusEntry(url="https://arxiv.org/abs/1234")
        assert entry.domain == "arxiv.org"

    def test_domain_extraction_with_port(self):
        """Test domain extraction handles ports."""
        entry = URLStatusEntry(url="https://localhost:8080/api")
        assert entry.domain == "localhost"

    def test_domain_extraction_invalid_url(self):
        """Test domain extraction falls back for invalid URLs."""
        entry = URLStatusEntry(url="not-a-valid-url")
        # Should not crash, will use fallback
        assert entry.domain is not None

    def test_entry_default_status_is_pending(self):
        """Test that default status is PENDING."""
        entry = URLStatusEntry(url="https://example.com")
        assert entry.status == URLStatus.PENDING

    def test_entry_with_custom_domain(self):
        """Test that custom domain overrides extraction."""
        entry = URLStatusEntry(url="https://example.com", domain="custom.com")
        assert entry.domain == "custom.com"

    def test_display_label_multi_segment_path(self):
        """Test display label with multi-segment path shows slug."""
        entry = URLStatusEntry(url="https://habr.com/ru/articles/123456/")
        assert entry.display_label == "habr.com/.../123456"

    def test_display_label_deep_path(self):
        """Test display label with deep path collapses to slug."""
        entry = URLStatusEntry(url="https://habr.com/ru/companies/co/blog/789/")
        assert entry.display_label == "habr.com/.../789"

    def test_display_label_root_path_only(self):
        """Test display label with root path returns domain only."""
        entry = URLStatusEntry(url="https://example.com/")
        assert entry.display_label == "example.com"

    def test_display_label_no_path(self):
        """Test display label with no path returns domain only."""
        entry = URLStatusEntry(url="https://example.com")
        assert entry.display_label == "example.com"

    def test_display_label_single_segment(self):
        """Test display label with single path segment."""
        entry = URLStatusEntry(url="https://example.com/article")
        assert entry.display_label == "example.com/article"

    def test_display_label_long_slug_truncation(self):
        """Test that long slugs are truncated in display label."""
        long_slug = "a" * 60
        entry = URLStatusEntry(url=f"https://medium.com/@user/{long_slug}")
        assert len(entry.display_label) <= 40
        assert entry.display_label.endswith("...")

    def test_display_label_same_domain_different_paths(self):
        """Test that same-domain URLs produce different labels."""
        entry1 = URLStatusEntry(url="https://habr.com/ru/articles/111/")
        entry2 = URLStatusEntry(url="https://habr.com/ru/articles/222/")
        assert entry1.display_label != entry2.display_label
        assert "111" in entry1.display_label
        assert "222" in entry2.display_label

    def test_display_label_strips_www(self):
        """Test that www. is stripped from display label."""
        entry = URLStatusEntry(url="https://www.example.com/page")
        assert entry.display_label == "example.com/page"


class TestURLBatchStatus(unittest.TestCase):
    """Test suite for URLBatchStatus class."""

    def test_from_urls_creates_entries(self):
        """Test creating batch from URL list."""
        urls = ["https://a.com", "https://b.com", "https://c.com"]
        batch = URLBatchStatus.from_urls(urls)

        assert len(batch.entries) == 3
        assert batch.total == 3
        assert all(e.status == URLStatus.PENDING for e in batch.entries)

    def test_mark_processing(self):
        """Test marking URL as processing."""
        batch = URLBatchStatus.from_urls(["https://example.com"])
        batch.mark_processing("https://example.com")

        assert batch.entries[0].status == URLStatus.PROCESSING
        assert batch.entries[0].start_time is not None

    def test_mark_complete(self):
        """Test marking URL as complete."""
        batch = URLBatchStatus.from_urls(["https://example.com"])
        batch.mark_processing("https://example.com")
        time.sleep(0.01)  # Small delay for timing
        batch.mark_complete("https://example.com", title="Test Article")

        entry = batch.entries[0]
        assert entry.status == URLStatus.COMPLETE
        assert entry.title == "Test Article"
        assert entry.processing_time_ms > 0

    def test_mark_failed(self):
        """Test marking URL as failed."""
        batch = URLBatchStatus.from_urls(["https://example.com"])
        batch.mark_processing("https://example.com")
        batch.mark_failed("https://example.com", "timeout", "Request timed out")

        entry = batch.entries[0]
        assert entry.status == URLStatus.FAILED
        assert entry.error_type == "timeout"
        assert entry.error_message == "Request timed out"

    def test_completed_property(self):
        """Test completed entries property."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com", "https://c.com"])
        batch.mark_complete("https://a.com", title="A")
        batch.mark_failed("https://b.com", "error", "failed")

        completed = batch.completed
        assert len(completed) == 1
        assert completed[0].url == "https://a.com"

    def test_failed_property(self):
        """Test failed entries property."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com"])
        batch.mark_complete("https://a.com", title="A")
        batch.mark_failed("https://b.com", "error", "failed")

        failed = batch.failed
        assert len(failed) == 1
        assert failed[0].url == "https://b.com"

    def test_pending_property(self):
        """Test pending entries property."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com", "https://c.com"])
        batch.mark_processing("https://a.com")

        pending = batch.pending
        assert len(pending) == 2

    def test_processing_property(self):
        """Test processing entries property."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com"])
        batch.mark_processing("https://a.com")

        processing = batch.processing
        assert len(processing) == 1
        assert processing[0].url == "https://a.com"

    def test_done_count(self):
        """Test done_count includes completed + failed."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com", "https://c.com"])
        batch.mark_complete("https://a.com", title="A")
        batch.mark_failed("https://b.com", "error", "failed")

        assert batch.done_count == 2
        assert batch.success_count == 1
        assert batch.fail_count == 1

    def test_average_processing_time(self):
        """Test average processing time calculation."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com"])
        batch.mark_complete("https://a.com", processing_time_ms=100.0)
        batch.mark_complete("https://b.com", processing_time_ms=200.0)

        avg = batch.average_processing_time_ms()
        assert avg == 150.0

    def test_average_processing_time_empty(self):
        """Test average processing time with no completed items."""
        batch = URLBatchStatus.from_urls(["https://a.com"])
        assert batch.average_processing_time_ms() == 0.0

    def test_estimate_remaining_time(self):
        """Test remaining time estimation (default concurrency=1)."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com", "https://c.com"])
        batch.mark_complete("https://a.com", processing_time_ms=1000.0)  # 1 second

        remaining = batch.estimate_remaining_time_sec()
        # 2 remaining URLs at 1s each, concurrency=1 -> 2 batches * 1s = 2s
        assert remaining is not None
        assert remaining == 2.0

    def test_estimate_remaining_time_with_concurrency(self):
        """Test that ETA accounts for parallel processing."""
        urls = [f"https://example{i}.com" for i in range(9)]
        batch = URLBatchStatus.from_urls(urls)
        batch.concurrency = 4
        # Complete first URL in 10 seconds
        batch.mark_complete("https://example0.com", processing_time_ms=10000.0)

        remaining = batch.estimate_remaining_time_sec()
        # 8 remaining, concurrency=4 -> ceil(8/4)=2 batches * 10s = 20s
        assert remaining is not None
        assert remaining == 20.0

    def test_estimate_remaining_time_no_data(self):
        """Test remaining time returns None with no timing data."""
        batch = URLBatchStatus.from_urls(["https://a.com"])
        assert batch.estimate_remaining_time_sec() is None

    def test_is_complete(self):
        """Test is_complete property."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com"])

        assert not batch.is_complete()

        batch.mark_complete("https://a.com", title="A")
        assert not batch.is_complete()

        batch.mark_failed("https://b.com", "error", "failed")
        assert batch.is_complete()


class TestBatchProgressFormatter(unittest.TestCase):
    """Test suite for BatchProgressFormatter."""

    def test_format_progress_empty_batch(self):
        """Test progress message for batch at start (HTML)."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com"])
        message = BatchProgressFormatter.format_progress_message(batch)

        assert "Processing 2 links..." in message
        assert "elapsed" in message
        assert '<a href="https://a.com">a.com</a>  Pending' in message
        assert '<a href="https://b.com">b.com</a>  Pending' in message
        assert "0/2" in message
        assert "Elapsed:" in message

    def test_format_progress_with_completed(self):
        """Test progress message shows completed entries as HTML links."""
        batch = URLBatchStatus.from_urls(["https://techcrunch.com/a", "https://arxiv.org/b"])
        batch.mark_complete("https://techcrunch.com/a", title="Article", processing_time_ms=1000)

        message = BatchProgressFormatter.format_progress_message(batch)

        assert '<a href="https://techcrunch.com/a">techcrunch.com/a</a>  Done (1s)' in message
        assert '<a href="https://arxiv.org/b">arxiv.org/b</a>  Pending' in message
        assert "1/2" in message

    def test_format_progress_with_processing(self):
        """Test progress message shows currently processing URL as HTML link."""
        batch = URLBatchStatus.from_urls(["https://example.com/article"])
        batch.mark_processing("https://example.com/article")

        message = BatchProgressFormatter.format_progress_message(batch)

        assert (
            '<a href="https://example.com/article">example.com/article</a>  Processing...'
            in message
        )

    def test_format_progress_shows_elapsed_time_in_header(self):
        """Test progress message shows elapsed time in header."""
        batch = URLBatchStatus.from_urls(["https://a.com"])
        message = BatchProgressFormatter.format_progress_message(batch)

        # Header should contain elapsed time
        first_line = message.split("\n")[0]
        assert "elapsed" in first_line

    def test_format_progress_shows_retrying_in_status_line(self):
        """Test progress message includes retrying entries with attempt info."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com"])
        batch.mark_processing("https://a.com")
        batch.mark_retrying("https://a.com", attempt=1, max_retries=2)

        message = BatchProgressFormatter.format_progress_message(batch)

        assert "Retrying (1/2)..." in message

    def test_format_progress_shows_retry_waiting_in_status_line(self):
        """Test progress message includes retry-waiting entries."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com"])
        batch.mark_processing("https://a.com")
        batch.mark_retrying("https://a.com", attempt=1, max_retries=3)
        batch.mark_retry_waiting("https://a.com")

        message = BatchProgressFormatter.format_progress_message(batch)

        assert "Waiting to retry (1/3)..." in message

    def test_format_progress_with_eta(self):
        """Test progress message shows ETA."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com", "https://c.com"])
        batch.mark_complete("https://a.com", processing_time_ms=30000)  # 30 seconds

        message = BatchProgressFormatter.format_progress_message(batch)

        assert "ETA:" in message or "Avg:" in message

    def test_format_completion_all_success(self):
        """Test completion message with all successful (HTML links)."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com"])
        batch.mark_complete("https://a.com", title="Article A", processing_time_ms=1000)
        batch.mark_complete("https://b.com", title="Article B", processing_time_ms=2000)

        message = BatchProgressFormatter.format_completion_message(batch)

        assert "<b>Batch Complete</b>  2/2 links" in message
        assert '1. <a href="https://a.com">Article A</a>' in message
        assert '2. <a href="https://b.com">Article B</a>' in message
        assert "Total:" in message
        assert "Avg:" in message

    def test_format_completion_with_failures(self):
        """Test completion message shows failed URLs as HTML links."""
        batch = URLBatchStatus.from_urls(["https://good.com", "https://bad.com"])
        batch.mark_complete("https://good.com", title="Good", processing_time_ms=1000)
        batch.mark_failed("https://bad.com", "timeout", "Timeout (30s)", processing_time_ms=30000)

        message = BatchProgressFormatter.format_completion_message(batch)

        assert "<b>Batch Complete</b>  1/2 links" in message
        assert '1. <a href="https://good.com">Good</a>' in message
        assert '2. <a href="https://bad.com">bad.com</a>  Failed: Timed out' in message

    def test_format_completion_truncates_long_titles(self):
        """Test that long titles are truncated."""
        batch = URLBatchStatus.from_urls(["https://example.com"])
        long_title = "A" * 100  # Very long title
        batch.mark_complete("https://example.com", title=long_title, processing_time_ms=1000)

        message = BatchProgressFormatter.format_completion_message(batch)

        # Should be truncated
        assert "..." in message

    def test_format_duration_seconds(self):
        """Test duration formatting for seconds."""
        assert BatchProgressFormatter._format_duration(0.5) == "<1s"
        assert BatchProgressFormatter._format_duration(30) == "30s"
        assert BatchProgressFormatter._format_duration(59) == "59s"

    def test_format_duration_minutes(self):
        """Test duration formatting for minutes."""
        assert BatchProgressFormatter._format_duration(60) == "1m"
        assert BatchProgressFormatter._format_duration(90) == "1m 30s"
        assert BatchProgressFormatter._format_duration(125) == "2m 5s"

    def test_format_duration_hours(self):
        """Test duration formatting for hours."""
        assert BatchProgressFormatter._format_duration(3600) == "1h 0m"
        assert BatchProgressFormatter._format_duration(3660) == "1h 1m"
        assert BatchProgressFormatter._format_duration(7200) == "2h 0m"

    def test_format_error_short_timeout(self):
        """Test error formatting for timeout."""
        error = BatchProgressFormatter._format_error_short("timeout", "Timeout (30s)")
        assert "Timed out" in error

    def test_format_error_short_network(self):
        """Test error formatting for network error."""
        error = BatchProgressFormatter._format_error_short("network", "Connection refused")
        assert error == "Network error"

    def test_format_error_short_truncates_long(self):
        """Test that long error messages are truncated."""
        long_error = "This is a very long error message that exceeds the limit"
        error = BatchProgressFormatter._format_error_short("error", long_error)
        assert len(error) <= 33  # 30 + "..."

    def test_message_length_under_limit(self):
        """Test that formatted messages stay under Telegram limit."""
        # Create a batch with many URLs
        urls = [f"https://example{i}.com/article" for i in range(20)]
        batch = URLBatchStatus.from_urls(urls)

        # Mark some as complete with long titles
        for i, url in enumerate(urls[:10]):
            batch.mark_complete(
                url, title=f"Very Long Article Title Number {i}", processing_time_ms=1000
            )

        # Mark some as failed
        for url in urls[10:15]:
            batch.mark_failed(url, "timeout", "Timeout after 90 seconds", processing_time_ms=90000)

        progress_msg = BatchProgressFormatter.format_progress_message(batch)
        completion_msg = BatchProgressFormatter.format_completion_message(batch)

        assert len(progress_msg) <= MAX_MESSAGE_LENGTH
        assert len(completion_msg) <= MAX_MESSAGE_LENGTH

    def test_get_current_processing_domain(self):
        """Test getting current processing domain."""
        batch = URLBatchStatus.from_urls(["https://example.com/article"])

        # Nothing processing yet
        assert BatchProgressFormatter.get_current_processing_domain(batch) is None

        batch.mark_processing("https://example.com/article")
        assert BatchProgressFormatter.get_current_processing_domain(batch) == "example.com"


class TestURLBatchStatusIndex(unittest.TestCase):
    """Test suite for URLBatchStatus URL index optimization."""

    def test_url_index_populated_on_creation(self):
        """Test that URL index is built by from_urls."""
        urls = ["https://a.com", "https://b.com", "https://c.com"]
        batch = URLBatchStatus.from_urls(urls)

        assert batch._url_index == {"https://a.com": 0, "https://b.com": 1, "https://c.com": 2}

    def test_find_entry_uses_index(self):
        """Test that _find_entry returns correct entry via index."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com"])
        entry = batch._find_entry("https://b.com")
        assert entry is not None
        assert entry.url == "https://b.com"

    def test_find_entry_missing_url(self):
        """Test that _find_entry returns None for unknown URL."""
        batch = URLBatchStatus.from_urls(["https://a.com"])
        assert batch._find_entry("https://missing.com") is None


class TestProgressBar(unittest.TestCase):
    """Test suite for progress bar formatting."""

    def test_progress_bar_empty(self):
        assert BatchProgressFormatter._format_progress_bar(0, 5) == "[----------] 0/5 (0%)"

    def test_progress_bar_half(self):
        assert BatchProgressFormatter._format_progress_bar(3, 5) == "[======----] 3/5 (60%)"

    def test_progress_bar_full(self):
        assert BatchProgressFormatter._format_progress_bar(5, 5) == "[==========] 5/5 (100%)"

    def test_progress_bar_zero_total(self):
        result = BatchProgressFormatter._format_progress_bar(0, 0)
        assert "0/0" in result

    def test_progress_bar_in_progress_message(self):
        """Test that progress message contains the text progress bar."""
        batch = URLBatchStatus.from_urls(["https://a.com", "https://b.com"])
        batch.mark_complete("https://a.com", title="A", processing_time_ms=1000)
        message = BatchProgressFormatter.format_progress_message(batch)
        assert "[=====-----] 1/2 (50%)" in message


class TestContentSizeInCompletion(unittest.TestCase):
    """Test content size display in completion message."""

    def test_completion_shows_content_size(self):
        batch = URLBatchStatus.from_urls(["https://a.com"])
        batch.mark_analyzing("https://a.com", content_length=15432)
        batch.mark_complete("https://a.com", title="Article", processing_time_ms=5000)
        message = BatchProgressFormatter.format_completion_message(batch)
        assert "15k chars" in message

    def test_completion_omits_size_when_missing(self):
        batch = URLBatchStatus.from_urls(["https://a.com"])
        batch.mark_complete("https://a.com", title="Article", processing_time_ms=5000)
        message = BatchProgressFormatter.format_completion_message(batch)
        assert "chars" not in message


class TestCompactProgress(unittest.TestCase):
    """Test compact progress format."""

    def test_compact_progress_shows_status_counts(self):
        urls = [f"https://ex{i}.com" for i in range(20)]
        batch = URLBatchStatus.from_urls(urls)
        batch.concurrency = 4
        for url in urls[:5]:
            batch.mark_complete(url, title="T", processing_time_ms=1000)
        for url in urls[5:8]:
            batch.mark_extracting(url)
        for url in urls[8:10]:
            batch.mark_analyzing(url)

        message = BatchProgressFormatter._format_compact_progress(batch)
        assert "<b>5</b> done" in message
        assert "3 extracting" in message
        assert "2 analyzing" in message
        assert "10 pending" in message


if __name__ == "__main__":
    unittest.main()
