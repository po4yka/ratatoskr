"""Optional ratatoskr.yaml loader.

The YAML file is the operator's authoritative on-disk config for non-secret
tunables.  Precedence (post-secret-marker refactor):

    non-secret YAML  >  os.environ  >  .env / ctor args  >  defaults
    secret env       >  defaults                (YAML secret keys ignored)

Secret-marked fields (see ``_secret_marker.py``) are stripped from YAML values
and logged as ``yaml_secret_keys_ignored``; place those in ``.env`` only.

Section names match ``Settings`` top-level attributes; field names match the
Pydantic field names within each sub-model. Validation aliases (UPPER_SNAKE
env-var names) are mapped automatically.

The loader returns either ``str`` values (for scalar/list fields that feed the
existing env-var validation path) or native ``dict`` objects for fields whose
annotation is ``dict[K, V]``. Pydantic's field validators for those fields
already accept the dict form — serialising to a JSON string and round-tripping
through the env-var parser would silently discard them.

This module also provides ``save_model_to_yaml`` and ``SECTION_MAP`` (previously
in the retired ``models_file`` module) for the ``/setmodel`` admin command, and
a ``load_models_yaml`` shim used by ``config_holder.ConfigReloader``.
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

# Reverse mapping: friendly /setmodel section name -> (yaml_section, yaml_key)
# Used by admin_handler to validate section arguments and by save_model_to_yaml.
SECTION_MAP: dict[str, tuple[str, str]] = {
    "primary": ("openrouter", "model"),
    "flash": ("openrouter", "flash_model"),
    "technical": ("model_routing", "technical_model"),
    "sociopolitical": ("model_routing", "sociopolitical_model"),
    "long_context": ("model_routing", "long_context_model"),
    "default": ("model_routing", "default_model"),
    "vision": ("attachment", "vision_model"),
}


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


def save_model_to_yaml(
    section: str,
    new_value: str,
    path: str | Path | None = None,
) -> tuple[str | None, str]:
    """Update a single model field in ratatoskr.yaml.

    Args:
        section: Friendly name (primary, flash, technical, etc.) from SECTION_MAP.
        new_value: New model identifier string.
        path: Override path to ratatoskr.yaml.  When *None*, the path is resolved
              via RATATOSKR_CONFIG or the DEFAULT_CONFIG_PATHS search order.

    Returns:
        (old_value, new_value) on success.

    Raises:
        ValueError: If section is unknown.
        FileNotFoundError: If ratatoskr.yaml cannot be located.
    """
    if section not in SECTION_MAP:
        msg = f"Unknown section '{section}'. Valid: {', '.join(sorted(SECTION_MAP))}"
        raise ValueError(msg)

    yaml_section, yaml_key = SECTION_MAP[section]

    # Resolve the config path the same way the loader does.
    config_path: Path | None
    if path is not None:
        config_path = Path(path)
    else:
        candidates = _candidate_paths(None)
        config_path = next((c for c in candidates if c.is_file()), None)

    if config_path is None or not config_path.is_file():
        raise FileNotFoundError(
            f"ratatoskr.yaml not found. "
            f"Set {CONFIG_PATH_ENV} or place the file at one of: "
            + ", ".join(str(p) for p in DEFAULT_CONFIG_PATHS)
        )

    import yaml

    raw = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}

    section_data = data.setdefault(yaml_section, {})
    old_value = section_data.get(yaml_key)
    section_data[yaml_key] = new_value

    config_path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )
    logger.info(
        "ratatoskr_yaml_model_updated",
        extra={"section": section, "old": old_value, "new": new_value},
    )
    return old_value, new_value


def load_models_yaml(path: str | Path | None = None) -> dict[str, str]:
    """Load model config from ratatoskr.yaml and return flat env-var-style dict.

    This is a forward-compat shim replacing the retired ``models_file`` module's
    ``load_models_yaml()``.  ``ConfigReloader`` calls this to poll for hot-reload
    changes; it now watches ratatoskr.yaml instead of the legacy models.yaml.

    The ``path`` argument and ``RATATOSKR_CONFIG`` env var are honoured for test
    overrides.  Falls back gracefully to an empty dict when the file is absent.

    Deprecated: callers should migrate to ``load_ratatoskr_yaml`` directly.
    """
    # Resolve the concrete file path.
    if path is not None:
        resolved: Path | None = Path(path) if Path(path).is_file() else None
    else:
        candidates = _candidate_paths(None)
        resolved = next((c for c in candidates if c.is_file()), None)

    if resolved is None:
        return {}

    data = _read_yaml(resolved)
    if not data:
        return {}

    # Re-use the same YAML→env mapping that models_file used (verbatim copy).
    _YAML_TO_ENV: dict[str, dict[str, str]] = {  # noqa: N806 — local alias of a constant-style mapping
        "openrouter": {
            "model": "OPENROUTER_MODEL",
            "fallback_models": "OPENROUTER_FALLBACK_MODELS",
            "long_context_model": "OPENROUTER_LONG_CONTEXT_MODEL",
            "flash_model": "OPENROUTER_FLASH_MODEL",
            "flash_fallback_models": "OPENROUTER_FLASH_FALLBACK_MODELS",
            "temperature": "OPENROUTER_TEMPERATURE",
            "top_p": "OPENROUTER_TOP_P",
            "max_tokens": "OPENROUTER_MAX_TOKENS",
            "enable_structured_outputs": "OPENROUTER_ENABLE_STRUCTURED_OUTPUTS",
            "structured_output_mode": "OPENROUTER_STRUCTURED_OUTPUT_MODE",
            "provider_order": "OPENROUTER_PROVIDER_ORDER",
            "enable_stats": "OPENROUTER_ENABLE_STATS",
            "enable_prompt_caching": "OPENROUTER_ENABLE_PROMPT_CACHING",
            "prompt_cache_ttl": "OPENROUTER_PROMPT_CACHE_TTL",
            "prompt_cache_ttl_anthropic": "OPENROUTER_PROMPT_CACHE_TTL_ANTHROPIC",
            "cache_system_prompt": "OPENROUTER_CACHE_SYSTEM_PROMPT",
            "cache_large_content_threshold": "OPENROUTER_CACHE_LARGE_CONTENT_THRESHOLD",
            "summary_temperature_relaxed": "OPENROUTER_SUMMARY_TEMPERATURE_RELAXED",
            "summary_top_p_relaxed": "OPENROUTER_SUMMARY_TOP_P_RELAXED",
            "summary_temperature_json_fallback": "OPENROUTER_SUMMARY_TEMPERATURE_JSON",
            "summary_top_p_json_fallback": "OPENROUTER_SUMMARY_TOP_P_JSON",
            "require_parameters": "OPENROUTER_REQUIRE_PARAMETERS",
            "auto_fallback_structured": "OPENROUTER_AUTO_FALLBACK_STRUCTURED",
            "max_response_size_mb": "OPENROUTER_MAX_RESPONSE_SIZE_MB",
        },
        "model_routing": {
            "enabled": "MODEL_ROUTING_ENABLED",
            "default_model": "MODEL_ROUTING_DEFAULT",
            "technical_model": "MODEL_ROUTING_TECHNICAL",
            "sociopolitical_model": "MODEL_ROUTING_SOCIOPOLITICAL",
            "long_context_model": "MODEL_ROUTING_LONG_CONTEXT",
            "fallback_models": "MODEL_ROUTING_FALLBACK_MODELS",
            "long_context_threshold_tokens": "MODEL_ROUTING_LONG_CONTEXT_THRESHOLD_TOKENS",
        },
        "attachment": {
            "vision_model": "ATTACHMENT_VISION_MODEL",
            "vision_fallback_models": "ATTACHMENT_VISION_FALLBACK_MODELS",
        },
    }

    def _serialize(value: Any) -> str:
        if isinstance(value, list):
            return ",".join(str(item) for item in value)
        if isinstance(value, bool):
            return str(value).lower()
        return str(value)

    result: dict[str, str] = {}
    for section_name, field_map in _YAML_TO_ENV.items():
        section = data.get(section_name)
        if not isinstance(section, dict):
            continue
        for yaml_key, env_name in field_map.items():
            if yaml_key in section:
                result[env_name] = _serialize(section[yaml_key])

    if result:
        logger.info(
            "ratatoskr_yaml_models_loaded",
            extra={"path": str(resolved), "keys_count": len(result)},
        )

    return result
