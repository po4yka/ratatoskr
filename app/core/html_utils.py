from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

from app.core.logging_utils import get_logger

logger = get_logger(__name__)

try:
    import trafilatura

    _HAS_TRAFILATURA = True
except Exception:  # pragma: no cover
    trafilatura = None
    _HAS_TRAFILATURA = False

# No longer using textacy - using built-in regex normalization


_BLANK_LINE_RE = re.compile(r"\n{3,}")


def _collapse_blank_lines(text: str) -> str:
    """Replace runs of three or more newlines with exactly two."""
    return _BLANK_LINE_RE.sub("\n\n", text)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf: list[str] = []
        self._skip_depth = 0  # skip script/style

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif self._skip_depth == 0:
            if tag in ("br",):
                self._buf.append("\n")
            elif tag in ("p", "div", "section", "article", "header", "footer"):
                self._buf.append("\n\n")
            elif tag in ("li",):
                self._buf.append("\n- ")
            elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self._buf.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and tag in ("p", "div"):
            self._buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._buf.append(text)

    def get_text(self) -> str:
        # Collapse excessive blank lines
        text = "".join(self._buf)
        text = unescape(text)
        # Normalize lines: remove 3+ newlines -> 2
        return _collapse_blank_lines(text).strip()


def html_to_text(html: str) -> str:
    # Prefer trafilatura for main content extraction if available
    if _HAS_TRAFILATURA and trafilatura is not None:
        try:
            # Extract main content with tables included, comments excluded
            text = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                no_fallback=False,
            )
            if text:
                # Normalize whitespace
                text = "\n".join(line.strip() for line in text.splitlines())
                text = _collapse_blank_lines(text)
                return text.strip()
        except Exception as e:
            logger.debug(
                "trafilatura_extraction_failed",
                extra={"error": str(e), "html_length": len(html)},
            )

    # Fallback: lightweight HTML parsing
    parser = _TextExtractor()
    try:
        parser.feed(html)
        return parser.get_text()
    except Exception as e:
        logger.debug(
            "html_parser_failed",
            extra={"error": str(e), "html_length": len(html)},
        )
        # Fallback: very naive strip
        import re

        txt = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
        txt = re.sub(r"<style[\s\S]*?</style>", "", txt, flags=re.IGNORECASE)
        txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.IGNORECASE)
        txt = re.sub(r"</p>", "\n\n", txt, flags=re.IGNORECASE)
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = unescape(txt)
        lines = [line.strip() for line in txt.splitlines() if line.strip()]
        return "\n".join(lines)


def clean_markdown_article_text(markdown: str | None) -> str:
    """Best-effort cleaning to extract only article text from markdown.

    - Removes code blocks and images
    - Converts links to plain text (keeps anchor text)
    - Drops common boilerplate/UI lines (share, login, embeds, etc.)
    - Collapses excessive whitespace
    """
    if not markdown:
        return ""

    text = markdown
    # Remove fenced code blocks
    text = re.sub(r"```[\s\S]*?```", "", text)
    # Remove inline code spans
    text = re.sub(r"`[^`]*`", "", text)
    # Remove images ![alt](url)
    text = re.sub(r"!\[[^\]]*\]\([^\)]+\)", "", text)
    # Convert links [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    # Remove reference-style link definitions and bare image refs
    text = re.sub(r"^\s*\[[^\]]+\]:\s*\S+\s*$", "", text, flags=re.MULTILINE)

    # Line-level filtering of boilerplate
    lines = [ln.rstrip() for ln in text.splitlines()]
    drop_prefixes = (
        "share",
        "watch later",
        "copy link",
        "include playlist",
        "tap to unmute",
        "you're signed out",
        "videos you watch",
        "search",
        "info",
        "shopping",
        "cancel",
        "confirm",
        "subscribe",
        "sign in",
        "login",
        "comments",
        "комментарии",
        "поделиться",
    )
    drop_exact = {"—", "-", "•", "* * *", "— — —"}

    filtered: list[str] = []
    for ln in lines:
        raw = ln.strip()
        if not raw:
            filtered.append("")
            continue
        low = raw.lower()
        if any(low.startswith(pfx) for pfx in drop_prefixes):
            continue
        if raw in drop_exact:
            continue
        # Drop plain URLs
        if re.fullmatch(r"https?://\S+", raw):
            continue
        # Drop lines that are mostly punctuation bullets
        if len(raw) <= 3 and raw.strip("•-*_") == "":
            continue
        filtered.append(raw)

    # Collapse multiple blank lines
    out_lines: list[str] = []
    prev_blank = False
    for ln in filtered:
        if ln == "":
            if not prev_blank:
                out_lines.append("")
            prev_blank = True
        else:
            out_lines.append(ln)
            prev_blank = False

    cleaned = "\n".join(out_lines).strip()
    # Normalize excessive spaces within lines
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    # Final collapse of triple newlines if any remain
    return _collapse_blank_lines(cleaned)


def normalize_text(text: str | None) -> str:
    """Apply lightweight text normalization.

    Operations:
    - normalize unicode quotes/dashes
    - replace URLs/emails/phone numbers with spaces
    - collapse repeated whitespace
    - strip control characters
    """
    if not text:
        return ""

    # Normalize unicode quotes and dashes
    out = text
    out = out.replace("'", "'").replace("'", "'")
    out = out.replace(""", '"').replace(""", '"')
    out = out.replace("—", "-").replace("–", "-")

    # Remove URLs, emails, and phone numbers
    out = re.sub(r"https?://\S+", " ", out)
    out = re.sub(r"\S+@\S+", " ", out)
    out = re.sub(r"\+?\d[\d\s\-\(\)]{7,}\d", " ", out)  # phone numbers

    # Remove control characters
    out = re.sub(r"[\u0000-\u001F\u007F]", " ", out)

    # Normalize whitespace
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = _collapse_blank_lines(out)

    return out.strip()


def chunk_sentences(sentences: list[str], max_chars: int = 2000) -> list[str]:
    """Group sentences into chunks under max_chars, preserving boundaries."""
    if not isinstance(sentences, list):
        return []
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for sent in sentences:
        s = (sent or "").strip()
        if not s:
            continue
        if size + len(s) + (1 if buf else 0) > max_chars and buf:
            chunks.append(" ".join(buf))
            buf = [s]
            size = len(s)
        else:
            if buf:
                size += 1  # space
            buf.append(s)
            size += len(s)
    if buf:
        chunks.append(" ".join(buf))
    return chunks
