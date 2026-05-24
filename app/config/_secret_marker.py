"""Per-field secret marker for the env-vs-YAML precedence model.

Convention (PR introducing this module):

* `.env` carries secrets ONLY (API keys, tokens, DB credentials, JWT signing
  keys, PII like ``ALLOWED_USER_IDS``).
* All other tunables live in `config/ratatoskr.yaml`.
* YAML overrides env for non-secret fields. Secret fields only accept env
  values; a secret found in YAML is logged and ignored so an operator never
  accidentally commits credentials.

To mark a Pydantic field as a secret, set ``json_schema_extra=SECRET_MARKER``
on its ``Field(...)`` call. The marker is read by the settings loader at
startup via ``collect_secret_env_names``.

Why a per-field marker (vs. a centralized allowlist):
* The marker lives next to the field definition, so reviewers see the
  classification at the call site.
* Renaming a field auto-keeps the classification.
* No drift between two source-of-truth lists.

The runtime check is intentionally lenient: any field whose
``json_schema_extra`` dict carries ``secret=True`` is treated as a secret.
Other entries in ``json_schema_extra`` (e.g. example values) are ignored.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import AliasChoices, BaseModel

if TYPE_CHECKING:
    from collections.abc import Iterable

# Sentinel value carried on every secret field. The dict layout matches the
# shape Pydantic's ``json_schema_extra`` accepts so it composes cleanly with
# any other JSON-schema metadata an author might add later.
SECRET_MARKER: dict[str, Any] = {"secret": True}


def is_secret_field(field_info: Any) -> bool:
    """Return True iff a Pydantic FieldInfo carries the secret marker."""
    extra = getattr(field_info, "json_schema_extra", None)
    if not isinstance(extra, dict):
        return False
    return bool(extra.get("secret"))


def _field_env_aliases(field_info: Any) -> list[str]:
    """Extract every env-var-style alias declared on a field."""
    names: list[str] = []
    alias = getattr(field_info, "validation_alias", None)
    if isinstance(alias, AliasChoices):
        for choice in alias.choices:
            if isinstance(choice, str):
                names.append(choice)
    elif isinstance(alias, str):
        names.append(alias)
    legacy_alias = getattr(field_info, "alias", None)
    if isinstance(legacy_alias, str) and legacy_alias not in names:
        names.append(legacy_alias)
    return names


def collect_secret_env_names(settings_model: type[BaseModel]) -> frozenset[str]:
    """Walk a settings-style model and collect every secret env-var name.

    Recurses one level into nested ``BaseModel`` fields (the standard layout
    used by ``Settings``: one section per Pydantic submodel). Two levels is
    sufficient because the project does not nest config sections deeper.
    """
    names: set[str] = set()
    for _section_name, section_field in settings_model.model_fields.items():
        annotation = section_field.annotation
        if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
            continue
        for _nested_name, nested_field in annotation.model_fields.items():
            if not is_secret_field(nested_field):
                continue
            for env_alias in _field_env_aliases(nested_field):
                names.add(env_alias)
    return frozenset(names)


def filter_yaml_to_non_secrets(
    yaml_data: dict[str, Any],
    secret_env_names: Iterable[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a flat env-var-style YAML dict into ``(non_secret, secret)``.

    The caller logs the secret half so operators get a single startup warning
    if credentials accidentally ended up in YAML.
    """
    secrets_set = frozenset(secret_env_names)
    non_secret: dict[str, Any] = {}
    secret: dict[str, Any] = {}
    for key, value in yaml_data.items():
        if key in secrets_set:
            secret[key] = value
        else:
            non_secret[key] = value
    return non_secret, secret
