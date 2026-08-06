"""Guard that rejects a non-Telethon session file before Telethon chokes on it.

Regression cover for a digest outage: a leftover Pyrogram session sat at the
digest userbot path, Telethon recognised it as its own from the ``version``
table, ran the 6->7 upgrade, and every scheduled run died on
``sqlite3.OperationalError: no such table: entities``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("telethon")

from telethon.sessions.sqlite import SQLiteSession

from app.adapters.telegram.compat_clients import _assert_usable_telethon_session


def _write_pyrogram_session(path: Path) -> None:
    """Build the schema Pyrogram writes -- ``version`` present, ``entities`` absent."""
    with sqlite3.connect(path) as conn:
        conn.execute("create table version (number integer primary key)")
        conn.execute("create table sessions (dc_id integer primary key, api_id integer)")
        conn.execute("create table peers (id integer primary key, access_hash integer)")
        conn.execute("create table usernames (id integer, username text)")
        conn.execute("create table update_state (id integer primary key, pts integer)")
        conn.execute("insert into version values (6)")


def test_rejects_pyrogram_session(tmp_path: Path) -> None:
    session = tmp_path / "channel_digest_userbot.session"
    _write_pyrogram_session(session)

    with pytest.raises(RuntimeError, match="not a usable Telethon session"):
        _assert_usable_telethon_session(str(session))


def test_rejects_pyrogram_session_when_path_omits_extension(tmp_path: Path) -> None:
    """Callers pass the extensionless path; Telethon appends ``.session`` itself."""
    _write_pyrogram_session(tmp_path / "channel_digest_userbot.session")

    with pytest.raises(RuntimeError, match="not a usable Telethon session"):
        _assert_usable_telethon_session(str(tmp_path / "channel_digest_userbot"))


def test_error_names_the_file_and_the_remedy(tmp_path: Path) -> None:
    session = tmp_path / "channel_digest_userbot.session"
    _write_pyrogram_session(session)

    with pytest.raises(RuntimeError) as excinfo:
        _assert_usable_telethon_session(str(session))

    message = str(excinfo.value)
    assert str(session) in message
    assert "/init_session" in message


def test_accepts_a_real_telethon_session(tmp_path: Path) -> None:
    """Built by Telethon itself, so the check tracks the real schema, not a copy of it."""
    session = tmp_path / "real.session"
    SQLiteSession(str(session)).close()

    _assert_usable_telethon_session(str(session))


def test_allows_missing_file(tmp_path: Path) -> None:
    """Telethon creates a fresh session at an unused path -- that is the /init_session flow."""
    _assert_usable_telethon_session(str(tmp_path / "not_created_yet"))


def test_rejects_a_file_that_is_not_sqlite(tmp_path: Path) -> None:
    session = tmp_path / "corrupt.session"
    session.write_text("this is not a database")

    with pytest.raises(RuntimeError, match="not a usable Telethon session"):
        _assert_usable_telethon_session(str(session))
