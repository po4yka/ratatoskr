"""Response formatter port for the application layer.

Defines the minimal Protocol surface that the LLM response workflow
orchestration depends on for sending notifications and presenting results.

The concrete implementation lives in ``app.adapters.external.formatting``,
which satisfies this protocol structurally.

``app.adapters.external.formatting.protocols`` re-exports
``ResponseFormatterPort`` as ``ResponseFormatterFacade`` so that adapter-side
callers that already import from that module are unchanged -- the same pattern
used by ``app.adapters.llm.protocol`` for ``LLMClientProtocol``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ResponseFormatterPort(Protocol):
    """Minimal formatter surface required by LLM orchestration services.

    The application-layer workflow only needs notification callbacks and error
    sending.  The full ``ResponseFormatterFacade`` (Telegram-specific rich
    formatting, topic routing, etc.) lives in the adapter layer and satisfies
    this protocol structurally.
    """

    async def send_error_notification(
        self,
        message: Any,
        error_type: str,
        correlation_id: str,
        details: str | None = None,
        reply_markup: Any | None = None,
    ) -> None:
        """Send error notification with rich formatting."""
        ...

    async def send_forward_completion_notification(self, message: Any, llm: Any) -> None:
        """Send forward completion notification."""
        ...

    async def send_llm_completion_notification(
        self, message: Any, llm: Any, correlation_id: str, *, silent: bool = False
    ) -> None:
        """Send LLM completion notification."""
        ...

    async def is_reader_mode(self, message: Any) -> bool:
        """Return whether the user prefers reader-mode progress updates."""
        ...

    async def send_message_draft(
        self,
        message: Any,
        text: str,
        *,
        message_thread_id: int | None = None,
        force: bool = False,
    ) -> bool:
        """Send a Telegram draft update if enabled."""
        ...

    def clear_message_draft(self, message: Any) -> None:
        """Clear request-level draft stream state."""
        ...

    def is_draft_streaming_enabled(self) -> bool:
        """Return whether draft-stream sending is enabled."""
        ...


__all__ = ["ResponseFormatterPort"]
