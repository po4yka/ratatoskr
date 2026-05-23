"""Welcome/help notification presenters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .notification_context import NotificationFormatterContext


class NotificationOnboardingPresenter:
    """Render simple onboarding/help messages."""

    def __init__(self, context: NotificationFormatterContext) -> None:
        self._context = context

    async def send_help(self, message: Any) -> None:
        help_text = (
            "Available Commands:\n"
            "  /start -- Welcome message and instructions\n"
            "  /help -- Show this help message\n"
            "  /summarize <URL> -- Summarize a URL\n"
            "  /summarize_all <URLs> -- Summarize multiple URLs from one message\n"
            "  /findweb <topic> -- Search the web (Firecrawl) for recent articles\n"
            "  /finddb <topic> -- Search your saved Ratatoskr library\n"
            "  /find <topic> -- Alias for /findweb\n"
            "  /cancel -- Cancel any pending URL or multi-link requests\n"
            "  /unread [topic] [limit] -- Show unread articles optionally filtered by topic\n"
            "  /read <ID> -- Mark article as read and view it\n"
            "  /social -- Show connected X, Instagram, and Threads account status\n"
            "  /connect_x -- Connect an X account\n"
            "  /connect_threads -- Connect a Threads account\n"
            "  /connect_instagram -- Connect an Instagram account\n"
            "  /disconnect_social <provider> -- Disconnect x, instagram, or threads\n"
            "  /dbinfo -- Show database overview\n"
            "  /dbverify -- Verify stored posts and required fields\n"
            "  /debug -- Toggle debug/reader notification mode\n\n"
            "Usage Tips:\n"
            "  Send URLs directly (commands are optional)\n"
            "  Forward channel posts to summarize them\n"
            "  Send /summarize and then a URL in the next message\n"
            "  Upload a .txt file with URLs (one per line) for batch processing\n"
            "  Multiple links in one message are supported\n"
            "  Use /unread [topic] [limit] to see saved articles by topic\n\n"
            "Features:\n"
            "  Structured JSON output with schema validation\n"
            "  Intelligent model fallbacks for better reliability\n"
            "  Automatic content optimization based on model capabilities\n"
            "  Silent batch processing for uploaded files\n"
            "  Progress tracking for multiple URLs"
        )
        await self._context.response_sender.safe_reply(message, help_text)

    async def send_welcome(self, message: Any) -> None:
        welcome = (
            "Welcome to Ratatoskr!\n\n"
            "What I do:\n"
            "- Summarize web articles via a multi-provider scraper chain "
            "(Scrapling → Crawl4AI → Firecrawl → Defuddle → Playwright → "
            "Crawlee → direct HTML → ScrapeGraph-AI fallback) and OpenRouter LLMs.\n"
            "- Download YouTube videos in 1080p and summarize their transcripts.\n"
            "- Extract and summarize Twitter/X tweets, threads, and Articles.\n"
            "- Auto-detect and summarize forwarded Telegram channel posts.\n"
            "- Optionally enrich summaries with current context via live web search.\n"
            "- Produce a 35+ field structured JSON document validated against a "
            "strict schema, with self-correction retries on validation failures.\n\n"
            "How to use:\n"
            "- Send a URL (article, YouTube, Twitter/X) directly, or use /summarize <URL>. "
            "You can also send /summarize and the URL in the next message.\n"
            "- Forward any Telegram channel post to me to summarize it — no command needed.\n"
            '- Multiple links in one message: I will ask "Process N links?" or use '
            "/summarize_all to skip the prompt.\n"
            "- /findweb <topic> for live web search; /finddb <topic> for semantic "
            "search across your saved library.\n"
            "- /unread, /read <ID> to manage your reading queue.\n"
            "- /channels, /subscribe, /digest to follow Telegram channels and "
            "receive scheduled digests of their posts.\n"
            "- /social to view connected X, Instagram, and Threads accounts, or "
            "use /connect_x, /connect_threads, and /connect_instagram to connect them.\n"
            "- /listen to generate audio from a summary.\n"
            "- /dbinfo, /dbverify, /admin for storage and operational stats.\n\n"
            "Notes:\n"
            "- All artifacts (sources, LLM calls, summaries, embeddings) are "
            "persisted to SQLite + Qdrant.\n"
            "- Errors include an Error ID you can reference in logs.\n"
            "- Full command list: /help."
        )
        await self._context.response_sender.safe_reply(message, welcome)
