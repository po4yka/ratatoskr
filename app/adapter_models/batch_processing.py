"""Result models for batch URL processing."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlparse


class URLStatus(StrEnum):
    """Status of a URL in batch processing."""

    PENDING = "pending"
    PROCESSING = "processing"
    EXTRACTING = "extracting"  # Firecrawl content extraction phase
    ANALYZING = "analyzing"  # Content analysis and model routing phase
    SUMMARIZING = "summarizing"  # LLM summary generation phase
    RETRYING = "retrying"  # Actively retrying
    RETRY_WAITING = "retry_waiting"  # Waiting for backoff cooldown
    COMPLETE = "complete"
    CACHED = "cached"  # Reused existing summary
    FAILED = "failed"


@dataclass
class URLStatusEntry:
    """Status entry for a single URL in batch processing.

    Tracks the current status, metadata, and timing for display in progress messages.

    Attributes:
        url: The URL being processed
        status: Current processing status
        domain: Extracted domain for compact display (e.g., "techcrunch.com")
        title: Article title (populated on completion)
        error_type: Type of error if failed
        error_message: Human-readable error message if failed
        processing_time_ms: Time taken to process in milliseconds
        start_time: Unix timestamp when processing started
    """

    url: str
    status: URLStatus = URLStatus.PENDING
    domain: str | None = None
    display_label: str | None = None
    title: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    processing_time_ms: float = 0.0
    start_time: float | None = None
    content_length: int | None = None
    model: str | None = None
    retry_count: int = 0
    max_retries: int = 0

    def __post_init__(self) -> None:
        """Extract domain and display label from URL on creation."""
        if self.domain is None:
            self.domain = self._extract_domain(self.url)
        if self.display_label is None:
            self.display_label = self._extract_display_label(self.url)

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract display-friendly domain from URL."""
        try:
            parsed = urlparse(url if "://" in url else f"https://{url}")
            host = parsed.hostname or parsed.netloc or url
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return url[:30]

    @staticmethod
    def _extract_display_label(url: str, max_length: int = 40) -> str:
        """Extract a display-friendly label that distinguishes same-domain URLs.

        Includes the last path segment (slug) to differentiate URLs from the same
        domain, e.g. ``habr.com/.../123456`` instead of just ``habr.com``.

        Args:
            url: The URL to extract the label from
            max_length: Maximum length of the returned label

        Returns:
            A compact, human-readable label like ``habr.com/.../123456``
        """
        try:
            parsed = urlparse(url if "://" in url else f"https://{url}")
            host = parsed.hostname or parsed.netloc or url
            if host.startswith("www."):
                host = host[4:]

            path = parsed.path.rstrip("/")
            segments = [segment for segment in path.split("/") if segment]

            if not segments:
                return host

            slug = segments[-1]
            label = f"{host}/{slug}" if len(segments) == 1 else f"{host}/.../{slug}"

            if len(label) > max_length:
                prefix = f"{host}/.../"
                available = max_length - len(prefix) - 3
                label = f"{prefix}{slug[:available]}..." if available > 0 else label[:max_length]

            return label
        except Exception:
            return url[:max_length]


@dataclass
class URLBatchStatus:
    """Status tracker for batch URL processing.

    Provides methods to update status, track timing, and calculate estimates.

    Attributes:
        entries: List of URLStatusEntry objects, one per URL
        batch_start_time: Unix timestamp when batch processing started
        _processing_times: List of completed processing times for ETA calculation
    """

    entries: list[URLStatusEntry] = field(default_factory=list)
    batch_start_time: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    concurrency: int = 1
    _processing_times: list[float] = field(default_factory=list, repr=False)
    _url_index: dict[str, int] = field(default_factory=dict, repr=False)

    @classmethod
    def from_urls(cls, urls: list[str]) -> URLBatchStatus:
        """Create a batch status tracker from a list of URLs."""
        entries = [URLStatusEntry(url=url) for url in urls]
        url_index = {url: index for index, url in enumerate(urls)}
        return cls(entries=entries, _url_index=url_index)

    def _update_timestamp(self) -> None:
        """Update last_updated timestamp."""
        self.last_updated = time.time()

    def _find_entry(self, url: str) -> URLStatusEntry | None:
        """Find entry by URL using O(1) index lookup."""
        idx = self._url_index.get(url)
        if idx is not None and idx < len(self.entries):
            return self.entries[idx]
        for entry in self.entries:
            if entry.url == url:
                return entry
        return None

    def mark_processing(self, url: str) -> None:
        """Mark a URL as currently processing."""
        entry = self._find_entry(url)
        if entry:
            entry.status = URLStatus.PROCESSING
            entry.start_time = time.time()
            self._update_timestamp()

    def mark_extracting(self, url: str) -> None:
        """Mark a URL as in the content extraction phase (Firecrawl)."""
        entry = self._find_entry(url)
        if entry:
            entry.status = URLStatus.EXTRACTING
            if entry.start_time is None:
                entry.start_time = time.time()
            self._update_timestamp()

    def mark_analyzing(
        self,
        url: str,
        title: str | None = None,
        content_length: int | None = None,
        model: str | None = None,
    ) -> None:
        """Mark a URL as in the LLM analysis phase."""
        entry = self._find_entry(url)
        if entry:
            entry.status = URLStatus.ANALYZING
            if title:
                entry.title = title
            if content_length:
                entry.content_length = content_length
            if model:
                entry.model = model
            self._update_timestamp()

    def mark_summarizing(
        self,
        url: str,
        model: str | None = None,
    ) -> None:
        """Mark a URL as in the LLM summary generation phase."""
        entry = self._find_entry(url)
        if entry:
            entry.status = URLStatus.SUMMARIZING
            if model:
                entry.model = model
            self._update_timestamp()

    def mark_retrying(
        self,
        url: str,
        *,
        attempt: int | None = None,
        max_retries: int | None = None,
    ) -> None:
        """Mark a URL as being retried."""
        entry = self._find_entry(url)
        if entry:
            entry.status = URLStatus.RETRYING
            if attempt is not None:
                entry.retry_count = attempt
            if max_retries is not None:
                entry.max_retries = max_retries
            self._update_timestamp()

    def mark_retry_waiting(self, url: str) -> None:
        """Mark a URL as waiting for a retry cooldown."""
        entry = self._find_entry(url)
        if entry:
            entry.status = URLStatus.RETRY_WAITING
            self._update_timestamp()

    def mark_complete(
        self,
        url: str,
        *,
        title: str | None = None,
        processing_time_ms: float | None = None,
    ) -> None:
        """Mark a URL as successfully completed."""
        entry = self._find_entry(url)
        if entry:
            entry.status = URLStatus.COMPLETE
            entry.title = title
            if processing_time_ms is not None:
                entry.processing_time_ms = processing_time_ms
            elif entry.start_time:
                entry.processing_time_ms = (time.time() - entry.start_time) * 1000
            if entry.processing_time_ms > 0:
                self._processing_times.append(entry.processing_time_ms)
            self._update_timestamp()

    def mark_cached(
        self,
        url: str,
        *,
        title: str | None = None,
    ) -> None:
        """Mark a URL as successfully reused from cache."""
        entry = self._find_entry(url)
        if entry:
            entry.status = URLStatus.CACHED
            entry.title = title
            entry.processing_time_ms = 0.0
            self._update_timestamp()

    def mark_failed(
        self,
        url: str,
        error_type: str,
        error_message: str,
        *,
        processing_time_ms: float | None = None,
    ) -> None:
        """Mark a URL as failed."""
        entry = self._find_entry(url)
        if entry:
            entry.status = URLStatus.FAILED
            entry.error_type = error_type
            entry.error_message = error_message
            if processing_time_ms is not None:
                entry.processing_time_ms = processing_time_ms
            elif entry.start_time:
                entry.processing_time_ms = (time.time() - entry.start_time) * 1000
            if entry.processing_time_ms > 0:
                self._processing_times.append(entry.processing_time_ms)
            self._update_timestamp()

    @property
    def total(self) -> int:
        """Total number of URLs in batch."""
        return len(self.entries)

    @property
    def completed(self) -> list[URLStatusEntry]:
        """List of successfully completed entries (including cached)."""
        return [
            entry
            for entry in self.entries
            if entry.status in {URLStatus.COMPLETE, URLStatus.CACHED}
        ]

    @property
    def failed(self) -> list[URLStatusEntry]:
        """List of failed entries."""
        return [entry for entry in self.entries if entry.status == URLStatus.FAILED]

    @property
    def pending(self) -> list[URLStatusEntry]:
        """List of pending entries."""
        return [entry for entry in self.entries if entry.status == URLStatus.PENDING]

    @property
    def processing(self) -> list[URLStatusEntry]:
        """List of currently processing entries (any active phase)."""
        active = {URLStatus.PROCESSING, URLStatus.EXTRACTING, URLStatus.ANALYZING}
        return [entry for entry in self.entries if entry.status in active]

    @property
    def done_count(self) -> int:
        """Number of URLs that are done (completed + cached + failed)."""
        return len(self.completed) + len(self.failed)

    @property
    def success_count(self) -> int:
        """Number of successfully completed URLs (including cached)."""
        return len(self.completed)

    @property
    def fail_count(self) -> int:
        """Number of failed URLs."""
        return len(self.failed)

    @property
    def pending_count(self) -> int:
        """Number of pending URLs."""
        return len(self.pending)

    def average_processing_time_ms(self) -> float:
        """Calculate average processing time in milliseconds."""
        if not self._processing_times:
            return 0.0
        return sum(self._processing_times) / len(self._processing_times)

    def estimate_remaining_time_sec(self) -> float | None:
        """Estimate remaining time in seconds based on average processing time.

        Accounts for parallel processing: remaining batches = ceil(remaining / concurrency).

        Returns:
            Estimated seconds remaining, or None if insufficient data
        """
        remaining = self.pending_count + len(self.processing)
        if remaining == 0:
            return 0.0

        avg_ms = self.average_processing_time_ms()
        if avg_ms <= 0:
            return None

        effective_concurrency = max(1, self.concurrency)
        parallel_batches = -(-remaining // effective_concurrency)
        return (parallel_batches * avg_ms) / 1000.0

    def total_elapsed_time_sec(self) -> float:
        """Calculate total elapsed time since batch started."""
        return time.time() - self.batch_start_time

    def is_complete(self) -> bool:
        """Check if all URLs have been processed."""
        return self.done_count >= self.total


__all__ = [
    "URLBatchStatus",
    "URLStatus",
    "URLStatusEntry",
]
