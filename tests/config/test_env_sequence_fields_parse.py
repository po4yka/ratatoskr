"""Setting an env-settable sequence config field must never abort startup.

Pydantic rejects a bare string for ``list[str]``/``tuple[str, ...]``, so a
sequence field carrying a ``validation_alias`` but no ``mode="before"`` parser
fails validation the moment an operator sets its env var. That failure is not
local to the feature: it propagates out of ``Settings(**overrides)`` as a
RuntimeError from ``load_config()``, which the bot, the Mobile API and the
Taskiq worker all call at boot. WEBWRIGHT_HOST_ALLOWLIST had no working value at
all -- following the documented double-gate to enable Webwright took the whole
deployment down.

The field set is derived from the config models rather than listed, because a
list would be the same incomplete answer that let this survive: three fields had
the defect while their immediate siblings (js_heavy_hosts, mirror_orgs,
agentic_pdf_host_allowlist) all had parsers.
"""

from __future__ import annotations

import importlib
import pkgutil
import typing
from typing import Any

import pytest
from pydantic import BaseModel

import app.config as config_package
from app.config.settings import load_config

pytestmark = pytest.mark.no_network

_SEQUENCE_ORIGINS = (tuple, list, set, frozenset)

# Enough to get load_config() past its required-value gate; unrelated to the
# fields under test.
_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "OPENROUTER_API_KEY": "test-key",
}


def _config_models() -> list[type[BaseModel]]:
    models: dict[str, type[BaseModel]] = {}
    for module_info in pkgutil.iter_modules(config_package.__path__):
        module = importlib.import_module(f"app.config.{module_info.name}")
        for attr in vars(module).values():
            if isinstance(attr, type) and issubclass(attr, BaseModel) and attr is not BaseModel:
                models[f"{attr.__module__}.{attr.__name__}"] = attr
    return list(models.values())


def _has_before_validator(model: type[BaseModel], field_name: str) -> bool:
    validators = model.__pydantic_decorators__.field_validators
    return any(
        decorator.info.mode == "before"
        and (field_name in decorator.info.fields or "*" in decorator.info.fields)
        for decorator in validators.values()
    )


def _env_sequence_fields() -> list[tuple[str, str, str]]:
    """(model name, field name, env var) for every env-settable sequence field."""
    found: list[tuple[str, str, str]] = []
    for model in _config_models():
        for field_name, field in model.model_fields.items():
            if typing.get_origin(field.annotation) not in _SEQUENCE_ORIGINS:
                continue
            alias = field.validation_alias
            if isinstance(alias, str):
                found.append((model.__name__, field_name, alias))
    return sorted(set(found))


_ENV_SEQUENCE_FIELDS = _env_sequence_fields()


def test_the_derivation_found_the_fields() -> None:
    """A derivation that matches nothing would make every assertion below vacuous."""
    env_vars = {env_var for _model, _field, env_var in _ENV_SEQUENCE_FIELDS}
    assert len(env_vars) >= 5, f"only found {sorted(env_vars)} -- the scan has gone stale"
    for expected in ("WEBWRIGHT_HOST_ALLOWLIST", "GIT_BACKUP_IGNORE", "SCRAPER_JS_HEAVY_HOSTS"):
        assert expected in env_vars, f"{expected} is env-settable and was not derived"


@pytest.mark.parametrize(
    ("model_name", "field_name", "env_var"),
    _ENV_SEQUENCE_FIELDS,
    ids=[f"{env_var}" for _m, _f, env_var in _ENV_SEQUENCE_FIELDS],
)
def test_sequence_field_has_a_string_parser(model_name: str, field_name: str, env_var: str) -> None:
    """Structural half: the field must coerce the string an env var can only be."""
    model = next(m for m in _config_models() if m.__name__ == model_name)
    assert _has_before_validator(model, field_name), (
        f"{model_name}.{field_name} is settable via {env_var} but has no "
        'mode="before" parser, so any value an operator sets fails validation '
        "and aborts load_config() for every process"
    )


@pytest.mark.parametrize(
    ("model_name", "field_name", "env_var"),
    _ENV_SEQUENCE_FIELDS,
    ids=[f"{env_var}" for _m, _f, env_var in _ENV_SEQUENCE_FIELDS],
)
def test_sequence_field_from_env_is_never_a_type_error(
    model_name: str, field_name: str, env_var: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behavioral half: the value must reach the field as a sequence, not a raw string.

    Probes with an empty JSON array because it is valid for every sequence type,
    which keeps this derived -- a per-field sample would need the hardcoded table
    this test exists to avoid. A field may still reject the value on domain
    grounds (INSTAGRAM_SCOPES requires a specific scope, for instance); that is a
    parsed value being judged, not the missing-parser defect. Only pydantic's own
    list_type/tuple_type errors mean the string never got parsed at all.
    """
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(env_var, "[]")

    try:
        load_config(allow_stub_telegram=True)
    except RuntimeError as exc:
        message = str(exc)
        assert "type=list_type" not in message and "type=tuple_type" not in message, (
            f"{model_name}.{field_name} never parsed the {env_var} string, so any "
            f"value an operator sets aborts load_config() for every process: {message}"
        )


@pytest.mark.parametrize(
    ("env_var", "value", "read", "expected"),
    [
        (
            "WEBWRIGHT_HOST_ALLOWLIST",
            "Example.COM, arxiv.org ,example.com",
            lambda cfg: cfg.scraper.webwright_host_allowlist,
            ("example.com", "arxiv.org"),
        ),
        (
            "WEBWRIGHT_HOST_ALLOWLIST",
            '["a.example", "b.example"]',
            lambda cfg: cfg.scraper.webwright_host_allowlist,
            ("a.example", "b.example"),
        ),
        (
            "WEBWRIGHT_HOST_ALLOWLIST",
            "*",
            lambda cfg: cfg.scraper.webwright_host_allowlist,
            ("*",),
        ),
        (
            "GIT_BACKUP_IGNORE",
            '["some-fork", "private/.*"]',
            lambda cfg: cfg.git_backup.ignore,
            ["some-fork", "private/.*"],
        ),
        (
            "GIT_BACKUP_IGNORE",
            "some-fork, private/.*",
            lambda cfg: cfg.git_backup.ignore,
            ["some-fork", "private/.*"],
        ),
    ],
)
def test_documented_env_syntax_loads(
    env_var: str,
    value: str,
    read: Any,
    expected: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every form the field's own description tells an operator to use must work."""
    for key, base_value in _BASE_ENV.items():
        monkeypatch.setenv(key, base_value)
    monkeypatch.setenv(env_var, value)

    assert read(load_config(allow_stub_telegram=True)) == expected


def test_git_backup_priorities_accepts_its_structured_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, base_value in _BASE_ENV.items():
        monkeypatch.setenv(key, base_value)
    monkeypatch.setenv("GIT_BACKUP_PRIORITIES", '[{"pattern": "acme/.*", "priority": 5}]')

    rules = load_config(allow_stub_telegram=True).git_backup.priorities

    assert [(rule.pattern, rule.priority) for rule in rules] == [("acme/.*", 5)]


def test_malformed_json_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad value must say which variable is wrong, not emit a bare type error."""
    for key, base_value in _BASE_ENV.items():
        monkeypatch.setenv(key, base_value)
    monkeypatch.setenv("WEBWRIGHT_HOST_ALLOWLIST", '["a",')

    with pytest.raises(RuntimeError, match="WEBWRIGHT_HOST_ALLOWLIST"):
        load_config(allow_stub_telegram=True)
