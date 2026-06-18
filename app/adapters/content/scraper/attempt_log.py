"""Per-attempt scraper telemetry payloads.

Each scraper provider call produces one :class:`ScraperAttemptEntry`.
A :class:`ScraperAttemptRecorder` collects entries in chain order so
the chain orchestrator can stash the full audit trail on the
``crawl_results`` row (column ``attempt_log`` JSON), letting operators
reconstruct multi-provider failure paths without scraping logs.
"""

from __future__ import annotations

from dataclasses import dataclass

_ALLOWED_STATUSES = frozenset({"success", "error", "timeout", "skipped"})


@dataclass(frozen=True)
class ScraperAttemptEntry:
    provider: str
    status: str
    latency_ms: int
    error_class: str | None
    error_message: str | None = None
    bytes_extracted: int | None = None

    def __post_init__(self) -> None:
        if self.status not in _ALLOWED_STATUSES:
            raise ValueError(
                f"unknown scraper status {self.status!r}; expected one of "
                + ", ".join(sorted(_ALLOWED_STATUSES))
            )


class ScraperAttemptRecorder:
    """Accumulator for one scraper chain run."""

    def __init__(self) -> None:
        self.entries: list[ScraperAttemptEntry] = []

    def record(self, entry: ScraperAttemptEntry) -> None:
        self.entries.append(entry)

    def winner(self) -> str | None:
        """Provider that produced the successful result, if any."""
        for entry in self.entries:
            if entry.status == "success":
                return entry.provider
        return None

    def failed_providers(self) -> list[str]:
        """Providers that did not produce the successful result.

        Excludes the winner. Includes timeouts and skipped providers.
        """
        win = self.winner()
        return [e.provider for e in self.entries if e.provider != win]


def serialize_attempt_log(
    entries: list[ScraperAttemptEntry] | tuple[ScraperAttemptEntry, ...],
) -> list[dict[str, object]]:
    """Render entries as a JSON-serializable list of dicts."""
    return [
        {
            "provider": entry.provider,
            "status": entry.status,
            "latency_ms": entry.latency_ms,
            "error_class": entry.error_class,
            "error_message": entry.error_message,
            "bytes_extracted": entry.bytes_extracted,
        }
        for entry in entries
    ]


__all__ = [
    "ScraperAttemptEntry",
    "ScraperAttemptRecorder",
    "serialize_attempt_log",
]
