from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = ROOT / "integrations/openclaw-skill"


def _registered_tool_names() -> set[str]:
    tree = ast.parse((ROOT / "app/mcp/tool_registrations.py").read_text())
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Name) and decorator.id == "contribute_tool"
            for decorator in node.decorator_list
        )
    }


def _registered_resource_uris() -> set[str]:
    tree = ast.parse((ROOT / "app/mcp/resource_registrations.py").read_text())
    uris: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == "_contribute_resource"
            ):
                uris.add(ast.literal_eval(decorator.args[1]))
    return uris


def test_openclaw_catalog_matches_registered_mcp_surface() -> None:
    config = json.loads((INTEGRATION / "config.json").read_text())
    tools = config["tools"]
    resources = config["resources"]

    assert len(tools) == len(set(tools)) == 28
    assert set(tools) == _registered_tool_names()
    assert len(resources) == len(set(resources)) == 17
    assert set(resources) == _registered_resource_uris()


def test_openclaw_skill_documents_every_catalog_entry() -> None:
    config = json.loads((INTEGRATION / "config.json").read_text())
    skill = (INTEGRATION / "SKILL.md").read_text()

    assert "28 tools and 17 resources" in skill
    for tool in config["tools"]:
        assert f"`{tool}`" in skill
    for resource in config["resources"]:
        assert f"`{resource}`" in skill


def test_openclaw_setup_uses_postgres_venv_and_explicit_scope() -> None:
    raw_config = (INTEGRATION / "config.json").read_text()
    config = json.loads(raw_config)
    skill = (INTEGRATION / "SKILL.md").read_text()

    assert config["mcp"]["command"] == ".venv/bin/python"
    assert "--user-id" in config["mcp"]["args"]
    assert "DATABASE_URL" in config["mcp"]["env"]
    assert '"DB_PATH"' not in raw_config
    assert "postgresql+asyncpg://" in skill
    assert "--auth-mode jwt --allow-remote-sse" in skill
