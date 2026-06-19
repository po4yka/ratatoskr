"""Save command -- save a URL to Ratatoskr."""

from __future__ import annotations

import click
from ratatoskr_cli.auth import get_client
from ratatoskr_cli.output import echo_success, format_json


@click.command()
@click.argument("url")
@click.option("--title", "-T", help="Custom title")
@click.option("--tag", "-t", multiple=True, help="Tags (repeatable)")
@click.option("--summarize/--no-summarize", default=True, help="Trigger summarization")
@click.option("--note", help="Note / selected text")
@click.pass_context
def save(
    ctx: click.Context,
    url: str,
    title: str | None,
    tag: tuple[str, ...],
    summarize: bool,
    note: str | None,
) -> None:
    """Save a URL to Ratatoskr."""
    client = get_client(ctx.obj)
    result = client.quick_save(
        url,
        title=title,
        tag_names=list(tag) or None,
        summarize=summarize,
        selected_text=note,
    )
    if ctx.obj["json"]:
        format_json(result)
    else:
        dup = result.get("duplicate", False)
        if dup:
            echo_success(f"Already saved (id={result.get('request_id')})")
        else:
            echo_success(f"Saved! Request ID: {result.get('request_id')}")
            tags = result.get("tags_pending") or result.get("tags_attached") or []
            if tags:
                click.echo(f"Tags (pending attachment): {', '.join(tags)}")
