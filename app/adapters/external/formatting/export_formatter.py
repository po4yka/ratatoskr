"""Export formatter for generating PDF, Markdown, and HTML exports of summaries."""

from __future__ import annotations

import asyncio
import html
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.infrastructure.persistence.repositories.request_repository import (
    RequestRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.summary_repository import (
    SummaryRepositoryAdapter,
)

from .export_temp_files import cleanup_export_file, named_export_temp_file

if TYPE_CHECKING:
    from app.db.session import Database

logger = get_logger(__name__)


class ExportFormatter:
    """Generates export files (PDF, Markdown, HTML) for summaries."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._request_repo = RequestRepositoryAdapter(db)
        self._summary_repo = SummaryRepositoryAdapter(db)

    def export_summary(
        self,
        summary_id: str,
        export_format: str,
        correlation_id: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Export a summary to the specified format.

        Args:
            summary_id: The summary ID to export
            export_format: One of 'pdf', 'md', 'html'
            correlation_id: Optional correlation ID for logging

        Returns:
            Tuple of (file_path, filename) or (None, None) if export failed
        """
        # Load summary from database
        summary_data = self._load_summary(summary_id)
        if not summary_data:
            logger.warning(
                "export_summary_not_found",
                extra={"summary_id": summary_id, "cid": correlation_id},
            )
            return None, None

        # Generate based on format
        if export_format == "pdf":
            return self._export_pdf(summary_data, correlation_id)
        if export_format == "md":
            return self._export_markdown(summary_data, correlation_id)
        if export_format == "html":
            return self._export_html(summary_data, correlation_id)

        logger.warning(
            "export_unknown_format",
            extra={"format": export_format, "cid": correlation_id},
        )
        return None, None

    def _load_summary(self, summary_id: str) -> dict[str, Any] | None:
        """Load summary data from database.

        The summary_id can be either:
        - A Summary.id (direct lookup)
        - A Request.id prefixed with 'req:' (lookup via request)
        """
        try:
            if summary_id.startswith("req:"):
                request_id = int(summary_id[4:])
                summary = asyncio.run(self._summary_repo.async_get_summary_by_request(request_id))
                if summary is None:
                    return None
                request = asyncio.run(self._request_repo.async_get_request_by_id(request_id))
            else:
                context = asyncio.run(
                    self._summary_repo.async_get_summary_context_by_id(int(summary_id))
                )
                if context is None:
                    return None
                summary = context.get("summary") if isinstance(context, dict) else None
                request = context.get("request") if isinstance(context, dict) else None

            if not isinstance(summary, dict):
                logger.debug("summary_not_found", extra={"summary_id": summary_id})
                return None

            url = request.get("normalized_url") if isinstance(request, dict) else None
            payload = summary.get("json_payload") or {}
            if not isinstance(payload, dict):
                payload = {}

            return {
                "id": str(summary.get("id")),
                "request_id": summary.get("request"),
                "url": url,
                "created_at": summary.get("created_at"),
                "lang": summary.get("lang"),
                **payload,
            }
        except Exception as e:
            logger.exception(
                "load_summary_failed", extra={"summary_id": summary_id, "error": str(e)}
            )
            return None

    def _export_markdown(
        self,
        data: dict[str, Any],
        correlation_id: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Export summary as Markdown file."""
        try:
            content = self._generate_markdown(data)
            filename = self._generate_filename(data, "md")

            fd_name: str | None = None
            try:
                with named_export_temp_file(mode="w", suffix=".md", encoding="utf-8") as fd:
                    fd.write(content)
                    fd_name = fd.name
            except Exception:
                if fd_name is not None:
                    cleanup_export_file(fd_name)
                raise

            logger.info(
                "export_markdown_generated",
                extra={"filename": filename, "cid": correlation_id},
            )
            return fd_name, filename

        except Exception as e:
            logger.exception(
                "export_markdown_failed", extra={"error": str(e), "cid": correlation_id}
            )
            return None, None

    def _export_html(
        self,
        data: dict[str, Any],
        correlation_id: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Export summary as HTML file."""
        try:
            content = self._generate_html(data)
            filename = self._generate_filename(data, "html")

            fd_name: str | None = None
            try:
                with named_export_temp_file(mode="w", suffix=".html", encoding="utf-8") as fd:
                    fd.write(content)
                    fd_name = fd.name
            except Exception:
                if fd_name is not None:
                    cleanup_export_file(fd_name)
                raise

            logger.info(
                "export_html_generated",
                extra={"filename": filename, "cid": correlation_id},
            )
            return fd_name, filename

        except Exception as e:
            logger.exception("export_html_failed", extra={"error": str(e), "cid": correlation_id})
            return None, None

    def _export_pdf(
        self,
        data: dict[str, Any],
        correlation_id: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Export summary as PDF file.

        Uses WeasyPrint if available, otherwise falls back to HTML with print stylesheet.
        """
        try:
            # First generate HTML content
            html_content = self._generate_html(data, for_pdf=True)
            filename = self._generate_filename(data, "pdf")

            # Try WeasyPrint first
            try:
                from weasyprint import HTML

                with named_export_temp_file(mode="wb", suffix=".pdf", encoding=None) as pdf_file:
                    pdf_file_name = pdf_file.name

                try:
                    HTML(string=html_content).write_pdf(pdf_file_name)
                except Exception:
                    cleanup_export_file(pdf_file_name)
                    raise

                logger.info(
                    "export_pdf_generated_weasyprint",
                    extra={"filename": filename, "cid": correlation_id},
                )
                return pdf_file_name, filename

            except ImportError:
                # WeasyPrint not available, return HTML with note
                logger.warning(
                    "export_pdf_weasyprint_unavailable",
                    extra={"cid": correlation_id},
                )
                # Fall back to HTML export with a note
                html_content_with_note = html_content.replace(
                    "</body>",
                    '<p style="color: #888; font-size: 12px;">'
                    "Note: PDF generation requires weasyprint. "
                    "Use browser print to save as PDF.</p></body>",
                )
                fd_name: str | None = None
                try:
                    with named_export_temp_file(mode="w", suffix=".html", encoding="utf-8") as fd:
                        fd.write(html_content_with_note)
                        fd_name = fd.name
                except Exception:
                    if fd_name is not None:
                        cleanup_export_file(fd_name)
                    raise

                # Change extension to indicate it's not a real PDF
                html_filename = filename.replace(".pdf", ".html")
                logger.info(
                    "export_pdf_fallback_html",
                    extra={"filename": html_filename, "cid": correlation_id},
                )
                return fd_name, html_filename

        except Exception as e:
            logger.exception("export_pdf_failed", extra={"error": str(e), "cid": correlation_id})
            return None, None

    def _generate_filename(self, data: dict[str, Any], extension: str) -> str:
        """Generate a descriptive filename for the export."""
        import re

        # Try to use SEO keywords or title
        seo = data.get("seo_keywords") or []
        if isinstance(seo, list) and seo:
            base = "-".join(self._slugify(str(x)) for x in seo[:3] if str(x).strip())
        else:
            # Use summary_250 first words
            summary = str(data.get("summary_250", "")).strip()
            if summary:
                words = re.findall(r"\w+", summary)[:5]
                base = "-".join(self._slugify(w) for w in words)
            else:
                base = "summary"

        base = base[:50] or "summary"
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        return f"{base}-{timestamp}.{extension}"

    def _slugify(self, text: str) -> str:
        """Convert text to URL-friendly slug."""
        import re

        text = text.strip().lower()
        text = re.sub(r"[^\w\-\s]", "", text)
        text = re.sub(r"[\s_]+", "-", text)
        text = re.sub(r"-+", "-", text).strip("-")
        return text[:30] if text else "export"

    def _generate_markdown(self, data: dict[str, Any]) -> str:
        """Generate Markdown content from summary data."""
        lines: list[str] = []

        # Title
        title = data.get("metadata", {}).get("title") or "Summary"
        lines.append(f"# {title}")
        lines.append("")

        # Source URL
        url = data.get("url")
        if url:
            lines.append(f"**Source:** [{url}]({url})")
            lines.append("")

        # TL;DR
        tldr = data.get("summary_250") or data.get("tldr")
        if tldr:
            lines.append("## TL;DR")
            lines.append("")
            lines.append(str(tldr).strip())
            lines.append("")

        # Full Summary
        summary_1000 = data.get("summary_1000")
        if summary_1000:
            lines.append("## Summary")
            lines.append("")
            lines.append(str(summary_1000).strip())
            lines.append("")

        # Key Ideas
        key_ideas = data.get("key_ideas") or []
        if key_ideas:
            lines.append("## Key Ideas")
            lines.append("")
            for idea in key_ideas:
                lines.append(f"- {str(idea).strip()}")
            lines.append("")

        # Topics/Tags
        tags = data.get("topic_tags") or []
        if tags:
            lines.append("## Topics")
            lines.append("")
            lines.append(" ".join(str(t) for t in tags))
            lines.append("")

        # Entities
        entities = data.get("entities") or {}
        if isinstance(entities, dict):
            people = entities.get("people") or []
            orgs = entities.get("organizations") or []
            locs = entities.get("locations") or []

            if people or orgs or locs:
                lines.append("## Entities")
                lines.append("")
                if people:
                    lines.append(f"**People:** {', '.join(str(p) for p in people)}")
                if orgs:
                    lines.append(f"**Organizations:** {', '.join(str(o) for o in orgs)}")
                if locs:
                    lines.append(f"**Locations:** {', '.join(str(loc) for loc in locs)}")
                lines.append("")

        # Key Stats
        key_stats = data.get("key_stats") or []
        if key_stats:
            lines.append("## Key Statistics")
            lines.append("")
            for stat in key_stats:
                if isinstance(stat, dict):
                    label = stat.get("label", "")
                    value = stat.get("value", "")
                    unit = stat.get("unit", "")
                    lines.append(f"- **{label}:** {value} {unit}".strip())
            lines.append("")

        # Reading Time
        reading_time = data.get("estimated_reading_time_min")
        if reading_time:
            lines.append(f"**Estimated Reading Time:** ~{reading_time} minutes")
            lines.append("")

        # SEO Keywords
        seo = data.get("seo_keywords") or []
        if seo:
            lines.append("## Keywords")
            lines.append("")
            lines.append(", ".join(str(k) for k in seo))
            lines.append("")

        # Footer
        lines.append("---")
        created = data.get("created_at")
        if created:
            if isinstance(created, datetime):
                created_str = created.strftime("%Y-%m-%d %H:%M UTC")
            else:
                created_str = str(created)
            lines.append(f"*Generated on {created_str} by Ratatoskr*")
        else:
            lines.append("*Generated by Ratatoskr*")

        return "\n".join(lines)

    def _generate_html(self, data: dict[str, Any], for_pdf: bool = False) -> str:
        """Generate HTML content from summary data."""
        title = html.escape(str(data.get("metadata", {}).get("title") or "Summary"))
        body_content = self._build_html_sections(data=data, escaped_title=title)
        return self._render_html_document(
            title=title,
            body_content=body_content,
            for_pdf=for_pdf,
        )

    def _build_html_sections(self, *, data: dict[str, Any], escaped_title: str) -> str:
        sections: list[str] = [f"<h1>{escaped_title}</h1>"]
        url = data.get("url")
        if url:
            safe_url = html.escape(str(url))
            sections.append(
                f'<p class="source"><strong>Source:</strong> <a href="{safe_url}">{safe_url}</a></p>'
            )

        self._append_summary_sections(sections, data)
        self._append_entities_section(sections, data.get("entities") or {})
        self._append_key_stats_section(sections, data.get("key_stats") or [])
        self._append_metadata_section(sections, data)
        self._append_footer_section(sections, data.get("created_at"))
        return "\n".join(sections)

    def _append_summary_sections(self, sections: list[str], data: dict[str, Any]) -> None:
        tldr = data.get("summary_250") or data.get("tldr")
        if tldr:
            sections.extend(
                [
                    '<div class="section tldr">',
                    "<h2>TL;DR</h2>",
                    f"<p>{html.escape(str(tldr).strip())}</p>",
                    "</div>",
                ]
            )

        summary_1000 = data.get("summary_1000")
        if summary_1000:
            sections.extend(
                [
                    '<div class="section summary">',
                    "<h2>Summary</h2>",
                    f"<p>{html.escape(str(summary_1000).strip())}</p>",
                    "</div>",
                ]
            )

        key_ideas = data.get("key_ideas") or []
        if key_ideas:
            sections.extend(['<div class="section key-ideas">', "<h2>Key Ideas</h2>", "<ul>"])
            for idea in key_ideas:
                sections.append(f"<li>{html.escape(str(idea).strip())}</li>")
            sections.extend(["</ul>", "</div>"])

        tags = data.get("topic_tags") or []
        if tags:
            sections.extend(
                ['<div class="section topics">', "<h2>Topics</h2>", '<div class="tags">']
            )
            for tag in tags:
                sections.append(f'<span class="tag">{html.escape(str(tag))}</span>')
            sections.extend(["</div>", "</div>"])

    def _append_entities_section(self, sections: list[str], entities: Any) -> None:
        if not isinstance(entities, dict):
            return
        people = entities.get("people") or []
        orgs = entities.get("organizations") or []
        locs = entities.get("locations") or []
        if not (people or orgs or locs):
            return
        sections.extend(['<div class="section entities">', "<h2>Entities</h2>"])
        if people:
            sections.append(
                f"<p><strong>People:</strong> {html.escape(', '.join(str(p) for p in people))}</p>"
            )
        if orgs:
            sections.append(
                f"<p><strong>Organizations:</strong> {html.escape(', '.join(str(o) for o in orgs))}</p>"
            )
        if locs:
            sections.append(
                f"<p><strong>Locations:</strong> {html.escape(', '.join(str(loc) for loc in locs))}</p>"
            )
        sections.append("</div>")

    def _append_key_stats_section(self, sections: list[str], key_stats: Any) -> None:
        if not key_stats:
            return
        sections.extend(['<div class="section stats">', "<h2>Key Statistics</h2>", "<ul>"])
        for stat in key_stats:
            if isinstance(stat, dict):
                label = html.escape(str(stat.get("label", "")))
                value = html.escape(str(stat.get("value", "")))
                unit = html.escape(str(stat.get("unit", "")))
                sections.append(f"<li><strong>{label}:</strong> {value} {unit}</li>")
        sections.extend(["</ul>", "</div>"])

    def _append_metadata_section(self, sections: list[str], data: dict[str, Any]) -> None:
        meta_parts: list[str] = []
        reading_time = data.get("estimated_reading_time_min")
        if reading_time:
            meta_parts.append(f"<li><strong>Reading Time:</strong> ~{reading_time} min</li>")
        seo = data.get("seo_keywords") or []
        if seo:
            meta_parts.append(
                f"<li><strong>Keywords:</strong> {html.escape(', '.join(str(k) for k in seo))}</li>"
            )
        if not meta_parts:
            return
        sections.extend(['<div class="section metadata">', "<h2>Metadata</h2>", "<ul>"])
        sections.extend(meta_parts)
        sections.extend(["</ul>", "</div>"])

    def _append_footer_section(self, sections: list[str], created: Any) -> None:
        if created:
            created_str = (
                created.strftime("%Y-%m-%d %H:%M UTC")
                if isinstance(created, datetime)
                else str(created)
            )
            sections.append(
                f"<footer><p>Generated on {html.escape(created_str)} by Ratatoskr</p></footer>"
            )
            return
        sections.append("<footer><p>Generated by Ratatoskr</p></footer>")

    def _render_html_document(self, *, title: str, body_content: str, for_pdf: bool) -> str:
        pdf_extra = ""
        if for_pdf:
            pdf_extra = """
            @page {
                size: A4;
                margin: 2cm;
            }
            """
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        {pdf_extra}
        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            line-height: 1.6;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            color: #333;
            background: #fff;
        }}
        h1 {{
            color: #1a1a1a;
            border-bottom: 2px solid #4A90D9;
            padding-bottom: 10px;
            margin-bottom: 20px;
        }}
        h2 {{
            color: #2c5282;
            margin-top: 30px;
            margin-bottom: 15px;
        }}
        .source {{
            color: #666;
            font-size: 14px;
            margin-bottom: 20px;
        }}
        .source a {{
            color: #4A90D9;
            text-decoration: none;
        }}
        .source a:hover {{
            text-decoration: underline;
        }}
        .section {{
            margin-bottom: 25px;
            padding: 15px;
            background: #f8f9fa;
            border-radius: 8px;
        }}
        .tldr {{
            background: #e8f4fd;
            border-left: 4px solid #4A90D9;
        }}
        .key-ideas ul {{
            padding-left: 20px;
        }}
        .key-ideas li {{
            margin-bottom: 8px;
        }}
        .tags {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .tag {{
            background: #4A90D9;
            color: white;
            padding: 4px 12px;
            border-radius: 15px;
            font-size: 14px;
        }}
        .stats ul {{
            list-style: none;
            padding: 0;
        }}
        .stats li {{
            padding: 8px 0;
            border-bottom: 1px solid #eee;
        }}
        .stats li:last-child {{
            border-bottom: none;
        }}
        footer {{
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #ddd;
            color: #888;
            font-size: 12px;
            text-align: center;
        }}
        @media print {{
            body {{
                max-width: none;
                padding: 0;
            }}
            .section {{
                break-inside: avoid;
            }}
        }}
    </style>
</head>
<body>
{body_content}
</body>
</html>"""
