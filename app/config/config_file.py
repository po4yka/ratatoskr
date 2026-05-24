"""Optional ratatoskr.yaml loader.

The YAML file is the operator's authoritative on-disk config. Non-secret YAML
values override matching env vars at startup (see ``_secret_marker.py``).
Section names match ``Settings`` top-level attributes; field names match the
Pydantic field names within each sub-model. Validation aliases (UPPER_SNAKE
env-var names) are mapped automatically.

The loader returns either ``str`` values (for scalar/list fields that feed the
existing env-var validation path) or native ``dict`` objects for fields whose
annotation is ``dict[K, V]``. Pydantic's field validators for those fields
already accept the dict form — serialising to a JSON string and round-tripping
through the env-var parser would silently discard them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, get_origin

from pydantic import AliasChoices, BaseModel

from app.core.logging_utils import get_logger

logger = get_logger(__name__)

CONFIG_PATH_ENV = "RATATOSKR_CONFIG"
DEFAULT_CONFIG_PATHS = (
    Path("ratatoskr.yaml"),
    Path("config/ratatoskr.yaml"),
    Path("/app/config/ratatoskr.yaml"),
)


def _is_dict_annotation(annotation: Any) -> bool:
    """Return True when *annotation* resolves to a ``dict[K, V]`` origin."""
    return get_origin(annotation) is dict


def _serialize_value(value: Any, annotation: Any = None) -> Any:
    """Convert a YAML value for injection into the Settings merge chain.

    For ``dict``-typed fields the value is returned as-is so the field's own
    validator receives the native dict (which it already handles).  All other
    types are serialised to a plain string matching the env-var convention.
    """
    if isinstance(value, dict) and annotation is not None and _is_dict_annotation(annotation):
        # Pass the dict through verbatim; the field validator handles it.
        return value
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list | tuple):
        return ",".join(str(item) for item in value)
    if isinstance(value, dict):
        # dict-typed field annotation not provided or not a plain dict[K,V] —
        # fall back to the comma-separated key=value string form.
        return ",".join(f"{k}={v}" for k, v in value.items())
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
            result[env_name] = _serialize_value(section[nested_name], nested_field.annotation)

    if result:
        logger.info(
            "ratatoskr_yaml_loaded",
            extra={"path": str(config_path), "keys_count": len(result)},
        )
    return result
