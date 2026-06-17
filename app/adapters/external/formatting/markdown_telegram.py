"""Render Markdown to Telegram-supported HTML.

Telethon drives the bot over MTProto, where outgoing formatting is carried by
``MessageEntity`` objects parsed from a *small* HTML whitelist
(``b/i/u/s/code/pre/a/tg-spoiler/blockquote/tg-emoji``). Telegram's newer
"rich text for bots" features (tables, checklists, headings, 32k Show-More)
are Bot-API-server constructs with no MTProto entity, so they cannot be emitted
here and are degraded gracefully (headings -> bold line, tables/raw HTML -> text).

The one genuinely-new feature reachable over MTProto is the **expandable
(collapsible) blockquote**, which Telethon's HTML parser exposes as
``<blockquote expandable>`` (verified: it sets ``MessageEntityBlockquote.collapsed``;
the ``collapsed`` attribute is NOT recognized -- only ``expandable``).

Parsing uses ``markdown-it-py`` (CommonMark) rather than regex so nested
emphasis, escapes, and code spans are handled correctly. All text content is
HTML-escaped; only whitelisted tags are emitted.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

# A blockquote (or wrapped section) longer than this many *visible* characters
# is rendered collapsed behind Telegram's "Show More" affordance.
EXPANDABLE_MIN_CHARS = 500

# Collapsing only helps when the body fits in one message; past the chunk limit
# it splits across messages anyway and only the first chunk would collapse, so a
# body this long is left as a plain (non-expandable) quote. This module-level
# default tracks the default max_message_chars (3900) minus a margin; callers
# with the live (config-driven) ceiling pass max_chars= to track it exactly.
EXPANDABLE_MAX_CHARS = 3800

# Schemes Telegram accepts in <a href>. Anything else drops the link, keeps text.
_SAFE_URL_PREFIXES = ("http://", "https://", "tg://", "mailto:")

_HEADING_PREFIX = {1: "▶ ", 2: "📌 "}  # h1/h2 get a marker; h3+ are plain bold
_TAG_RE = re.compile(r"<[^>]+>")


@lru_cache(maxsize=1)
def _parser() -> MarkdownIt:
    """CommonMark parser with strikethrough enabled, HTML passthrough disabled."""
    md = MarkdownIt("commonmark", {"html": False})
    try:  # strikethrough (~~x~~) is registered but disabled in the commonmark preset
        md.enable("strikethrough")
    except (ValueError, KeyError):  # pragma: no cover - preset always has the rule
        pass
    return md


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _esc_attr(text: str) -> str:
    return html.escape(text or "", quote=True)


def _safe_url(href: str | None) -> bool:
    return (href or "").strip().lower().startswith(_SAFE_URL_PREFIXES)


def _visible_len(rendered_html: str) -> int:
    return len(_TAG_RE.sub("", rendered_html))


def blockquote(content: str, *, escape: bool = True, expandable: bool = False) -> str:
    """Wrap *content* in a Telegram blockquote, optionally collapsible."""
    body = _esc(content) if escape else content
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"{tag}{body}</blockquote>"


def maybe_expandable_blockquote(
    content: str,
    *,
    escape: bool = True,
    threshold: int = EXPANDABLE_MIN_CHARS,
    max_chars: int = EXPANDABLE_MAX_CHARS,
) -> str:
    """Blockquote that collapses only for a body in the [threshold, max_chars] band.

    Below *threshold* it isn't worth collapsing; above *max_chars* the body
    splits across messages anyway (the collapse would apply to only the first
    chunk), so emit a plain blockquote instead. Pass *max_chars* derived from the
    live per-message ceiling so the band tracks it.
    """
    visible = len(content) if escape else _visible_len(content)
    return blockquote(content, escape=escape, expandable=threshold < visible <= max_chars)


def render_markdown(md_text: str, *, expandable_threshold: int = EXPANDABLE_MIN_CHARS) -> str:
    """Convert a Markdown string to Telegram-supported HTML."""
    text = (md_text or "").strip()
    if not text:
        return ""
    root = SyntaxTreeNode(_parser().parse(text))
    out = _join_blocks(root.children, expandable_threshold)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _join_blocks(nodes: list[SyntaxTreeNode], threshold: int) -> str:
    parts = [_render_block(n, threshold) for n in nodes]
    return "\n\n".join(p for p in parts if p.strip())


def _render_block(node: SyntaxTreeNode, threshold: int) -> str:
    kind = node.type
    if kind == "paragraph":
        return _render_inline_container(node)
    if kind == "heading":
        level = int(node.tag[1]) if node.tag[1:].isdigit() else 3
        return f"<b>{_HEADING_PREFIX.get(level, '')}{_render_inline_container(node)}</b>"
    if kind == "blockquote":
        inner = _join_blocks(node.children, threshold)
        expandable = threshold < _visible_len(inner) <= EXPANDABLE_MAX_CHARS
        return blockquote(inner, escape=False, expandable=expandable)
    if kind in ("bullet_list", "ordered_list"):
        return _render_list(node, threshold)
    if kind in ("fence", "code_block"):
        return _render_code(node)
    if kind == "hr":
        return "———"
    if kind in ("html_block", "html_inline"):
        return _esc(node.content)
    # Unknown container: fall back to its children / raw content (escaped).
    if node.children:
        return _join_blocks(node.children, threshold)
    return _esc(node.content)


def _render_inline_container(node: SyntaxTreeNode) -> str:
    """Render a block whose single child is an ``inline`` token (paragraph/heading)."""
    parts = []
    for child in node.children:
        parts.append(_render_inline(child) if child.type == "inline" else _render_block(child, 0))
    return "".join(parts)


def _render_inline(node: SyntaxTreeNode) -> str:
    out: list[str] = []
    for child in node.children:
        kind = child.type
        if kind == "text":
            out.append(_esc(child.content))
        elif kind == "strong":
            out.append(f"<b>{_render_inline(child)}</b>")
        elif kind == "em":
            out.append(f"<i>{_render_inline(child)}</i>")
        elif kind == "s":
            out.append(f"<s>{_render_inline(child)}</s>")
        elif kind == "code_inline":
            out.append(f"<code>{_esc(child.content)}</code>")
        elif kind == "link":
            inner = _render_inline(child)
            href = child.attrs.get("href", "") if child.attrs else ""
            out.append(
                f'<a href="{_esc_attr(str(href))}">{inner}</a>' if _safe_url(str(href)) else inner
            )
        elif kind == "image":
            alt = child.content or "".join(c.content for c in child.children if c.type == "text")
            src = child.attrs.get("src", "") if child.attrs else ""
            if _safe_url(str(src)):
                out.append(f'<a href="{_esc_attr(str(src))}">{_esc(alt) or "🖼"}</a>')
            else:
                out.append(_esc(alt))
        elif kind in ("softbreak", "hardbreak"):
            out.append("\n")
        elif kind in ("html_inline", "html_block"):
            out.append(_esc(child.content))
        elif child.children:
            out.append(_render_inline(child))
        else:
            out.append(_esc(child.content))
    return "".join(out)


def _render_list(node: SyntaxTreeNode, threshold: int) -> str:
    ordered = node.type == "ordered_list"
    start = 1
    if ordered and node.attrs and "start" in node.attrs:
        try:
            start = int(node.attrs["start"])
        except (TypeError, ValueError):
            start = 1
    lines: list[str] = []
    index = start
    for item in node.children:
        if item.type != "list_item":
            continue
        inner = _join_blocks(item.children, threshold).strip()
        marker = f"{index}. " if ordered else "• "
        if "<pre>" in inner or "<blockquote" in inner:
            # Newlines inside <pre>/<blockquote> are content; indenting them
            # would inject spurious leading spaces into code/quote bodies.
            lines.append(marker + inner)
        else:
            first, *rest = inner.split("\n")
            lines.append(marker + first)
            lines.extend("   " + line for line in rest)  # indent nested-list lines
        index += 1
    return "\n".join(lines)


def _render_code(node: SyntaxTreeNode) -> str:
    code = _esc(node.content.rstrip("\n"))
    lang = (node.info or "").strip().split(" ", 1)[0] if node.type == "fence" else ""
    if lang:
        return f'<pre><code class="language-{_esc_attr(lang)}">{code}</code></pre>'
    return f"<pre>{code}</pre>"


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    sample = (
        "# Title\n\nSome **bold**, _italic_, ~~strike~~ and `code`.\n\n"
        "[link](https://example.com) and a bad [js](javascript:alert(1)) link.\n\n"
        "> a quote with <script> & ampersand\n\n"
        "- one\n- two\n\n```python\nprint('hi')\n```\n"
    )
    print(render_markdown(sample))
    assert "javascript:" not in render_markdown(sample)
    assert "&lt;script&gt;" in render_markdown(sample)
    print("\nOK")
