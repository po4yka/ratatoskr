"""Start the mobile API with parent-owned Prometheus multiprocess setup."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


def _bounded_int(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def run_api_server(
    *,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., Any] | None = None,
) -> None:
    """Prepare metrics once in the parent, then start Uvicorn workers."""
    source = os.environ if environ is None else environ
    host = source.get("API_HOST", "0.0.0.0").strip()
    if not host:
        raise ValueError("API_HOST must not be empty")
    port = _bounded_int(source, "API_PORT", 8000, minimum=1, maximum=65535)
    workers = _bounded_int(source, "API_WORKERS", 1, minimum=1, maximum=32)
    multiprocess_directory = source.get("PROMETHEUS_MULTIPROC_DIR", "").strip()
    if workers > 1 and not multiprocess_directory:
        raise ValueError(
            "PROMETHEUS_MULTIPROC_DIR is required when API_WORKERS is greater than 1"
        )
    if multiprocess_directory:
        # Imported only after the deployment has set PROMETHEUS_MULTIPROC_DIR.
        # Clearing here happens once in Uvicorn's parent, before it imports the
        # application in child workers.
        from app.observability.metrics_http import prepare_multiprocess_directory

        prepare_multiprocess_directory(source)

    if runner is None:
        import uvicorn

        runner = uvicorn.run
    runner(
        "app.api.main:app",
        host=host,
        port=port,
        workers=workers,
    )


def main() -> None:
    try:
        run_api_server()
    except ValueError as exc:
        raise SystemExit(f"API server configuration error: {exc}") from exc


if __name__ == "__main__":  # pragma: no cover
    main()
