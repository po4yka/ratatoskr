from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.cli.api_server import run_api_server


def test_single_worker_local_mode_does_not_require_multiprocess_env() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    run_api_server(
        environ={"API_HOST": "127.0.0.1", "API_PORT": "8080", "API_WORKERS": "1"},
        runner=lambda app, **kwargs: calls.append((app, kwargs)),
    )

    assert calls == [
        (
            "app.api.main:app",
            {"host": "127.0.0.1", "port": 8080, "workers": 1},
        )
    ]


def test_multiple_workers_require_multiprocess_directory() -> None:
    with pytest.raises(ValueError, match="PROMETHEUS_MULTIPROC_DIR is required"):
        run_api_server(environ={"API_WORKERS": "2"}, runner=lambda *_args, **_kwargs: None)


def test_parent_clears_metrics_before_starting_uvicorn(tmp_path: Path) -> None:
    stale = tmp_path / "counter_123.db"
    stale.write_text("stale", encoding="utf-8")
    calls: list[tuple[str, dict[str, Any]]] = []

    def _runner(app: str, **kwargs: Any) -> None:
        assert not stale.exists()
        calls.append((app, kwargs))

    run_api_server(
        environ={
            "API_WORKERS": "3",
            "PROMETHEUS_MULTIPROC_DIR": str(tmp_path),
        },
        runner=_runner,
    )

    assert calls[0][1]["workers"] == 3


@pytest.mark.parametrize(
    ("name", "value"),
    [("API_PORT", "0"), ("API_PORT", "invalid"), ("API_WORKERS", "33")],
)
def test_launcher_rejects_invalid_numeric_config(name: str, value: str) -> None:
    with pytest.raises(ValueError):
        run_api_server(environ={name: value}, runner=lambda *_args, **_kwargs: None)
