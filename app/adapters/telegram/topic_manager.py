"""Forum topic management for private chat DM topics.

Organizes summaries into categorized topics within private bot conversations.
Requires Telegram Bot API support for DM topics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from typing import Protocol

    class Client(Protocol):
        pass


logger = get_logger(__name__)

# Default forum topic categories with associated keyword patterns.
# Each tuple: (topic_name, icon_color, keyword_set).
# icon_color values: 0x6FB9F0 (blue), 0xFFD67E (yellow), 0xCB86DB (purple),
#   0x8EEE98 (green), 0xFF93B2 (pink), 0xFB6F5F (red)
DEFAULT_TOPICS: list[tuple[str, int, frozenset[str]]] = [
    (
        "Tech & Programming",
        0x6FB9F0,
        frozenset(
            {
                "technology",
                "programming",
                "software",
                "coding",
                "developer",
                "engineering",
                "api",
                "python",
                "javascript",
                "typescript",
                "rust",
                "golang",
                "database",
                "cloud",
                "devops",
                "linux",
                "web",
                "framework",
                "open-source",
                "algorithm",
                "frontend",
                "backend",
                "cybersecurity",
                "infosec",
            }
        ),
    ),
    (
        "AI & Machine Learning",
        0xCB86DB,
        frozenset(
            {
                "ai",
                "artificial-intelligence",
                "machine-learning",
                "ml",
                "deep-learning",
                "neural-network",
                "nlp",
                "llm",
                "gpt",
                "transformer",
                "computer-vision",
                "data-science",
                "generative-ai",
                "chatbot",
                "model",
            }
        ),
    ),
    (
        "Science & Research",
        0x8EEE98,
        frozenset(
            {
                "science",
                "research",
                "physics",
                "chemistry",
                "biology",
                "mathematics",
                "astronomy",
                "space",
                "climate",
                "environment",
                "neuroscience",
                "genetics",
                "quantum",
                "academic",
                "study",
            }
        ),
    ),
    (
        "Business & Finance",
        0xFFD67E,
        frozenset(
            {
                "business",
                "finance",
                "economy",
                "startup",
                "investment",
                "market",
                "stock",
                "crypto",
                "cryptocurrency",
                "blockchain",
                "venture-capital",
                "ipo",
                "revenue",
                "profit",
                "strategy",
                "management",
                "leadership",
                "entrepreneurship",
            }
        ),
    ),
    (
        "News & Current Events",
        0xFB6F5F,
        frozenset(
            {
                "news",
                "politics",
                "geopolitics",
                "regulation",
                "law",
                "policy",
                "government",
                "election",
                "conflict",
                "diplomacy",
                "society",
                "breaking",
                "current-events",
            }
        ),
    ),
    (
        "Health & Lifestyle",
        0xFF93B2,
        frozenset(
            {
                "health",
                "medicine",
                "fitness",
                "nutrition",
                "wellness",
                "mental-health",
                "psychology",
                "pharma",
                "medical",
                "disease",
                "therapy",
                "lifestyle",
                "food",
                "travel",
                "sports",
                "hobby",
            }
        ),
    ),
]

# Fallback topic for summaries that don't match any category.
GENERAL_TOPIC_NAME = "General"
GENERAL_TOPIC_COLOR = 0x6FB9F0


class TopicManager:
    """Manages forum topics in private bot conversations.

    Keeps an in-memory cache of topic_name -> topic_id per chat.
    Topics are lazily created on first use.
    """

    def __init__(self) -> None:
        # chat_id -> {topic_name -> topic_id}
        self._topics: dict[int, dict[str, int]] = {}
        # chat_id -> bool (whether DM topics are enabled for this chat)
        self._enabled_chats: set[int] = set()

    async def ensure_dm_topics_enabled(self, client: Client, chat_id: int) -> bool:
        """Enable DM topics for a private chat if not already enabled.

        Returns True if topics are (now) enabled, False on failure.
        """
        if chat_id in self._enabled_chats:
            return True
        try:
            client_any: Any = client
            await client_any.set_chat_direct_messages_group(chat_id, is_enabled=True)
            self._enabled_chats.add(chat_id)
            logger.info("dm_topics_enabled", extra={"chat_id": chat_id})
            return True
        except Exception as exc:
            logger.warning(
                "dm_topics_enable_failed",
                extra={"chat_id": chat_id, "error": str(exc)},
            )
            return False

    async def ensure_default_topics(self, client: Client, chat_id: int) -> None:
        """Create default topic categories if they don't exist yet."""
        if chat_id in self._topics:
            return

        if not await self.ensure_dm_topics_enabled(client, chat_id):
            return

        topic_map: dict[str, int] = {}
        client_any: Any = client

        # First, try to load existing topics
        try:
            existing = []
            async for topic in client_any.get_direct_messages_topics(chat_id):
                existing.append(topic)
            for topic in existing:
                topic_name = getattr(topic, "name", None) or getattr(topic, "title", None)
                topic_id = getattr(topic, "topic_id", None) or getattr(topic, "id", None)
                if topic_name and topic_id:
                    topic_map[topic_name] = topic_id
        except Exception as exc:
            logger.debug(
                "dm_topics_list_failed",
                extra={"chat_id": chat_id, "error": str(exc)},
            )

        # Create missing default topics
        for topic_name, icon_color, _keywords in DEFAULT_TOPICS:
            if topic_name in topic_map:
                continue
            try:
                result = await client_any.create_forum_topic(
                    chat_id, topic_name, icon_color=icon_color
                )
                # create_forum_topic returns a Message; extract the topic_id
                topic_id = getattr(result, "message_thread_id", None)
                if topic_id is None:
                    # Fallback: some versions return the topic differently
                    topic_id = getattr(result, "id", None)
                if topic_id:
                    topic_map[topic_name] = topic_id
                    logger.debug(
                        "dm_topic_created",
                        extra={"chat_id": chat_id, "topic": topic_name, "topic_id": topic_id},
                    )
            except Exception as exc:
                logger.warning(
                    "dm_topic_create_failed",
                    extra={"chat_id": chat_id, "topic": topic_name, "error": str(exc)},
                )

        # Create general fallback topic
        if GENERAL_TOPIC_NAME not in topic_map:
            try:
                result = await client_any.create_forum_topic(
                    chat_id, GENERAL_TOPIC_NAME, icon_color=GENERAL_TOPIC_COLOR
                )
                topic_id = getattr(result, "message_thread_id", None) or getattr(result, "id", None)
                if topic_id:
                    topic_map[GENERAL_TOPIC_NAME] = topic_id
            except Exception as exc:
                logger.debug(
                    "dm_general_topic_create_failed",
                    extra={"chat_id": chat_id, "error": str(exc)},
                )

        self._topics[chat_id] = topic_map
        logger.info(
            "dm_topics_initialized",
            extra={"chat_id": chat_id, "topic_count": len(topic_map)},
        )

    def resolve_topic_id(self, chat_id: int, topic_tags: list[str]) -> int | None:
        """Map summary topic_tags to the best matching forum topic ID.

        Args:
            chat_id: The chat to look up topics for.
            topic_tags: List of tags from the summary JSON (e.g. ["#technology", "#python"]).

        Returns:
            The topic_id for the best matching category, or the General topic,
            or None if topics aren't initialized for this chat.
        """
        topic_map = self._topics.get(chat_id)
        if not topic_map:
            return None

        # Normalize tags: strip # prefix, lowercase
        normalized = {tag.lstrip("#").lower().replace(" ", "-") for tag in topic_tags if tag}

        # Score each category by keyword overlap
        best_name = GENERAL_TOPIC_NAME
        best_score = 0

        for topic_name, _color, keywords in DEFAULT_TOPICS:
            if topic_name not in topic_map:
                continue
            score = len(normalized & keywords)
            if score > best_score:
                best_score = score
                best_name = topic_name

        return topic_map.get(best_name)
