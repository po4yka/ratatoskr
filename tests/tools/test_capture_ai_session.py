"""Security tests for the operator AI-session capture helper."""

from __future__ import annotations

import json
import os
import stat

import pytest

from tools.scripts.capture_ai_session import _write_session_state


def test_session_state_is_written_owner_only(tmp_path) -> None:
    out = tmp_path / "chatgpt.json"
    state = {"cookies": [{"name": "session", "value": "secret"}]}
    _write_session_state(out, state)

    assert json.loads(out.read_text()) == state
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_session_state_replaces_regular_file_with_owner_only_mode(tmp_path) -> None:
    out = tmp_path / "claude.json"
    out.write_text("old")
    out.chmod(0o644)
    _write_session_state(out, {"cookies": []})

    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert json.loads(out.read_text()) == {"cookies": []}


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="symlink guard requires O_NOFOLLOW")
def test_session_state_refuses_symlink_destination(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("untouched")
    out = tmp_path / "session.json"
    out.symlink_to(target)

    with pytest.raises(OSError, match="symlink"):
        _write_session_state(out, {"cookies": [{"value": "secret"}]})
    assert target.read_text() == "untouched"
