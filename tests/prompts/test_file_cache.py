from __future__ import annotations

from pathlib import Path

from app.prompts.file_cache import clear_prompt_file_cache, read_prompt_text


def test_read_prompt_text_reads_file_only_once(tmp_path: Path) -> None:
    target = tmp_path / "prompt.txt"
    target.write_text("  hello world  ", encoding="utf-8")
    clear_prompt_file_cache()

    assert read_prompt_text(target) == "  hello world  "
    assert read_prompt_text(target, strip=True) == "hello world"

    # Mutating the file on disk does NOT change the cached value (read-once).
    target.write_text("changed", encoding="utf-8")
    assert read_prompt_text(target) == "  hello world  "

    # Clearing the cache forces a fresh read.
    clear_prompt_file_cache()
    assert read_prompt_text(target) == "changed"


def test_prompt_manager_does_not_reread_on_cache_hit(tmp_path: Path, monkeypatch) -> None:
    from app.prompts import manager as manager_module

    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    # A minimal prompt that passes validation (mentions required fields + JSON).
    fields = " ".join(sorted(manager_module.REQUIRED_PROMPT_FIELDS))
    (prompt_dir / "summary_system_en.txt").write_text(
        f"Return only JSON. {fields} " + ("x" * 120),
        encoding="utf-8",
    )

    mgr = manager_module.PromptManager(prompt_dir=prompt_dir, validate_on_load=False)

    read_calls = 0
    real_read_text = Path.read_text

    def _counting_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal read_calls
        if self.name == "summary_system_en.txt":
            read_calls += 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _counting_read_text)

    first = mgr.get_system_prompt("en", include_examples=False)
    second = mgr.get_system_prompt("en", include_examples=False)

    assert first == second
    # The file is read exactly once: the second call is served from cache via a
    # cheap mtime stat, with no second read_text (the old code re-hashed the file
    # by reading its bytes on every call).
    assert read_calls == 1
