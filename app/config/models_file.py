"""Load model configuration from a YAML file.

Provides a flat dict of env-var-style keys populated from a nested YAML
structure.  The result is injected into the Settings merge chain at the
lowest priority layer (env vars and constructor args override it).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.core.logging_utils import get_logger

logger = get_logger(__name__)

_DEFAULT_PATH = "config/models.yaml"

# Mapping: YAML section -> {yaml_key: ENV_VAR_NAME}
_YAML_TO_ENV: dict[str, dict[str, str]] = {
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
    "openai": {
        "model": "OPENAI_MODEL",
        "fallback_models": "OPENAI_FALLBACK_MODELS",
        "enable_structured_outputs": "OPENAI_ENABLE_STRUCTURED_OUTPUTS",
    },
    "anthropic": {
        "model": "ANTHROPIC_MODEL",
        "fallback_models": "ANTHROPIC_FALLBACK_MODELS",
        "enable_structured_outputs": "ANTHROPIC_ENABLE_STRUCTURED_OUTPUTS",
    },
    "attachment": {
        "vision_model": "ATTACHMENT_VISION_MODEL",
        "vision_fallback_models": "ATTACHMENT_VISION_FALLBACK_MODELS",
    },
}


def _serialize_value(value: Any) -> str:
    """Convert a YAML value to a string suitable for env-var consumption."""
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def load_models_yaml(path: str | Path | None = None) -> dict[str, str]:
    """Load model config from YAML and return flat env-var-style dict.

    Returns an empty dict if the file does not exist (graceful degradation).
    The path can be overridden via the ``MODELS_CONFIG_PATH`` env var.
    """
    if path is None:
        path = os.environ.get("MODELS_CONFIG_PATH", _DEFAULT_PATH)

    config_path = Path(path)
    if not config_path.is_file():
        return {}

    try:
        import yaml

        raw = config_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except Exception:
        logger.warning("models_yaml_parse_failed", extra={"path": str(config_path)})
        return {}

    if not isinstance(data, dict):
        logger.warning("models_yaml_invalid_root", extra={"path": str(config_path)})
        return {}

    result: dict[str, str] = {}
    for section_name, field_map in _YAML_TO_ENV.items():
        section = data.get(section_name)
        if not isinstance(section, dict):
            continue
        for yaml_key, env_name in field_map.items():
            if yaml_key in section:
                result[env_name] = _serialize_value(section[yaml_key])

    if result:
        logger.info(
            "models_yaml_loaded",
            extra={"path": str(config_path), "keys_count": len(result)},
        )

    return result


# Reverse mapping: friendly name -> (yaml_section, yaml_key)
_SECTION_MAP: dict[str, tuple[str, str]] = {
    "primary": ("openrouter", "model"),
    "flash": ("openrouter", "flash_model"),
    "technical": ("model_routing", "technical_model"),
    "sociopolitical": ("model_routing", "sociopolitical_model"),
    "long_context": ("model_routing", "long_context_model"),
    "default": ("model_routing", "default_model"),
    "vision": ("attachment", "vision_model"),
}


def save_model_to_yaml(
    section: str,
    new_value: str,
    path: str | Path | None = None,
) -> tuple[str | None, str]:
    """Update a single model field in models.yaml.

    Args:
        section: Friendly name (primary, flash, technical, etc.)
        new_value: New model identifier string.
        path: Override path to models.yaml.

    Returns:
        (old_value, new_value) on success.

    Raises:
        ValueError: If section is unknown.
        FileNotFoundError: If models.yaml does not exist.
    """
    if section not in _SECTION_MAP:
        msg = f"Unknown section '{section}'. Valid: {', '.join(sorted(_SECTION_MAP))}"
        raise ValueError(msg)

    yaml_section, yaml_key = _SECTION_MAP[section]

    if path is None:
        import os

        path = os.environ.get("MODELS_CONFIG_PATH", _DEFAULT_PATH)

    config_path = Path(path)
    if not config_path.is_file():
        msg = f"Models config not found: {config_path}"
        raise FileNotFoundError(msg)

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
        "models_yaml_updated",
        extra={"section": section, "old": old_value, "new": new_value},
    )
    return old_value, new_value
