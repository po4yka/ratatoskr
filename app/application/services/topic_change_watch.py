"""Build provenance-preserving briefs for changes in a user's active topics."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.time_utils import UTC

if TYPE_CHECKING:
    from datetime import datetime


def build_topic_change_brief(
    *,
    topic_name: str,
    signals: list[dict[str, Any]],
    since: datetime | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return one concise change brief and its persisted source provenance."""
    if not signals:
        return "", []
    since_text = since.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if since else "the first run"
    lines = [f"Topic watch: {topic_name}", f"Changes since {since_text}:"]
    provenance: list[dict[str, Any]] = []
    for signal in signals:
        signal_id = int(signal["signal_id"])
        title = str(signal.get("title") or "Untitled source").strip()
        url = str(signal.get("url") or "").strip()
        score = signal.get("final_score")
        score_text = f" (score {float(score):.2f})" if isinstance(score, (float, int)) else ""
        lines.append(f"- {title}{score_text} [signal:{signal_id}]")
        if url:
            lines.append(f"  {url}")
        provenance.append(
            {
                "signal_id": signal_id,
                "feed_item_id": int(signal["feed_item_id"]),
                "source_id": int(signal["source_id"]),
                "url": url or None,
                "final_score": float(score) if isinstance(score, (float, int)) else None,
            }
        )
    return "\n".join(lines), provenance
