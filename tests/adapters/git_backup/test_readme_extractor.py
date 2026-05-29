"""Characterization tests for ReadmeExtractor (port of ReadmeExtractorTest.kt).

Uses real git to build bare repos, mirroring the Kotlin test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.adapters.git_backup.readme_extractor import ReadmeExtractor


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        check=True,
    )


def _commit_repo(source: Path, files: dict[str, str]) -> None:
    _git("init", "-q", cwd=source)
    _git("config", "user.email", "test@test.com", cwd=source)
    _git("config", "user.name", "Test", cwd=source)
    for name, content in files.items():
        (source / name).write_text(content)
        _git("add", name, cwd=source)
    _git("commit", "-q", "-m", "init", cwd=source)


def _bare_repo_with(tmp_path: Path, files: dict[str, str]) -> Path:
    source = tmp_path / "source"
    bare = tmp_path / "bare.git"
    source.mkdir()
    _commit_repo(source, files)
    _git("clone", "--mirror", "-q", str(source), str(bare))
    return bare


@pytest.fixture
def extractor() -> ReadmeExtractor:
    return ReadmeExtractor()


def test_extracts_readme_md(tmp_path: Path, extractor: ReadmeExtractor) -> None:
    content = "# My Project\n\nThis is the README."
    bare = _bare_repo_with(tmp_path, {"README.md": content})
    assert extractor.extract(bare) == content


def test_falls_back_to_lowercase_readme(tmp_path: Path, extractor: ReadmeExtractor) -> None:
    content = "# lowercase readme"
    bare = _bare_repo_with(tmp_path, {"readme.md": content})
    assert extractor.extract(bare) == content


def test_falls_back_to_readme_rst(tmp_path: Path, extractor: ReadmeExtractor) -> None:
    content = "My Project\n==========\n\nA reStructuredText readme."
    bare = _bare_repo_with(tmp_path, {"README.rst": content})
    assert extractor.extract(bare) == content


def test_empty_when_no_readme(tmp_path: Path, extractor: ReadmeExtractor) -> None:
    bare = _bare_repo_with(tmp_path, {"main.py": "print('hi')\n"})
    assert extractor.extract(bare) == ""


def test_truncates_to_8000_chars(tmp_path: Path, extractor: ReadmeExtractor) -> None:
    content = "A" * 10000
    bare = _bare_repo_with(tmp_path, {"README.md": content})
    result = extractor.extract(bare)
    assert len(result) == 8000
    assert result == content[:8000]


def test_empty_for_bare_repo_with_no_commits(tmp_path: Path, extractor: ReadmeExtractor) -> None:
    bare = tmp_path / "empty.git"
    _git("init", "--bare", "-q", str(bare))
    assert extractor.extract(bare) == ""
