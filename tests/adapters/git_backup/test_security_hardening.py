"""Security-hardening tests for the git-backup engine.

Covers credential redaction in error/log text and the SSRF redirect-hardening
flag in the git argv builder.
"""

from __future__ import annotations

from app.adapters.git_backup.git_commands import build_git_command
from app.adapters.git_backup.mirror_service import _redact_url


def test_redact_url_strips_token_from_https() -> None:
    out = _redact_url(
        "fatal: unable to access 'https://x-access-token:ghp_secret@github.com/o/r.git/'"
    )
    assert "ghp_secret" not in out
    assert "x-access-token" not in out
    assert "https://***@github.com/o/r.git" in out


def test_redact_url_strips_all_occurrences_multiline() -> None:
    text = "remote: https://user:tok1@github.com/a\nremote: ssh://git:tok2@host.example/b\n"
    out = _redact_url(text)
    assert "tok1" not in out
    assert "tok2" not in out
    assert out.count("***@") == 2


def test_redact_url_leaves_clean_urls_untouched() -> None:
    clean = "cloning https://github.com/owner/repo.git"
    assert _redact_url(clean) == clean


def test_build_git_command_disables_redirects_when_requested() -> None:
    argv = build_git_command(
        repo_exists=False,
        url="https://github.com/o/r.git",
        repo_name="r.git",
        disable_redirects=True,
    )
    joined = " ".join(argv)
    assert "http.followRedirects=false" in joined


def test_build_git_command_omits_redirect_flag_by_default() -> None:
    argv = build_git_command(
        repo_exists=False,
        url="https://github.com/o/r.git",
        repo_name="r.git",
    )
    assert "http.followRedirects=false" not in " ".join(argv)
