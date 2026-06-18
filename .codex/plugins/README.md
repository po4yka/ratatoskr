# Codex Plugins

This repository has no checked-in Claude Code plugin manifests to translate. Claude Code marketplace/plugin flows such as `/plugin` and `/reload-plugins` are host-specific; Codex should use repo-local skills from `.codex/skills/`, prompt aliases from `.codex/commands/`, and any runtime plugin tools exposed by the Codex environment.

If a future Claude plugin is added under `.claude/`, add the Codex-facing behavior here only when there is a real Codex plugin manifest or tool configuration to maintain. Otherwise, mirror the shared workflow as a skill.
