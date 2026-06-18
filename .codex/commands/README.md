# Codex Commands

Codex does not load Claude Code slash-command files directly. The files in this directory are Codex prompt aliases for the repo's command workflows; paste or invoke the matching `@...` trigger in Codex, and use the `.codex/skills/<name>/SKILL.md` skill body as the source of truth.

The Claude Code equivalents live in `.claude/commands/`. Keep both trees aligned when changing command behavior.
