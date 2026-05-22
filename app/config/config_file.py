"""Optional ratatoskr.yaml loader.

The YAML file is a power-user layer below environment variables. It uses the
same top-level section names as ``Settings`` and nested field names from each
Pydantic config model, then returns env-var-style keys so the existing
validation path remains canonical.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, BaseModel

from app.core.logging_utils import get_logger

logger = get_logger(__name__)

CONFIG_PATH_ENV = "RATATOSKR_CONFIG"
DEFAULT_CONFIG_PATHS = (
    Path("ratatoskr.yaml"),
    Path("config/ratatoskr.yaml"),
    Path("/app/config/ratatoskr.yaml"),
)


def _serialize_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list | tuple):
        return ",".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    return str(value)


def _field_aliases(field: Any) -> list[str]:
    aliases: list[str] = []
    alias = field.validation_alias
    if isinstance(alias, AliasChoices):
        aliases.extend(choice for choice in alias.choices if isinstance(choice, str))
    elif isinstance(alias, str):
        aliases.append(alias)
    if field.alias:
        aliases.append(field.alias)
    return aliases


def _primary_env_alias(field: Any) -> str | None:
    aliases = _field_aliases(field)
    return aliases[0] if aliases else None


def _candidate_paths(path: str | Path | None) -> tuple[Path, ...]:
    if path is not None:
        return (Path(path),)

    explicit = os.environ.get(CONFIG_PATH_ENV)
    if explicit:
        return (Path(explicit),)

    return DEFAULT_CONFIG_PATHS


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml

        raw = path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(raw) or {}
    except Exception:
        logger.warning("ratatoskr_yaml_parse_failed", extra={"path": str(path)})
        return {}

    if not isinstance(loaded, dict):
        logger.warning("ratatoskr_yaml_invalid_root", extra={"path": str(path)})
        return {}
    return loaded


def load_ratatoskr_yaml(
    settings_model: type[BaseModel],
    path: str | Path | None = None,
) -> dict[str, str]:
    """Load optional ``ratatoskr.yaml`` and return env-var-style values.

    Unknown sections and keys are ignored deliberately. The resulting dict is
    fed into the existing Settings validator below ``.env`` and process env.
    """
    config_path = next(
        (candidate for candidate in _candidate_paths(path) if candidate.is_file()), None
    )
    if config_path is None:
        return {}

    data = _read_yaml(config_path)
    if not data:
        return {}

    result: dict[str, str] = {}
    for section_name, field_info in settings_model.model_fields.items():
        if section_name == "allow_stub_telegram":
            continue
        section = data.get(section_name)
        if not isinstance(section, dict):
            continue

        annotation = field_info.annotation
        if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
            continue

        for nested_name, nested_field in annotation.model_fields.items():
            if nested_name not in section:
                continue
            env_name = _primary_env_alias(nested_field)
            if env_name is None:
                continue
            result[env_name] = _serialize_value(section[nested_name])

    if result:
        logger.info(
            "ratatoskr_yaml_loaded",
            extra={"path": str(config_path), "keys_count": len(result)},
        )
    return result
