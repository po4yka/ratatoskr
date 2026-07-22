"""Push UI-installed credentials into the live config snapshot.

Bridges :class:`~app.infrastructure.persistence.credential_store.CredentialStore`
to the existing ``ConfigHolder`` hot-reload path that ``/setmodel`` already
uses: rebuild the affected config sections, ``swap()`` them in, and let the
registered listeners re-read the values they froze at construction. Nothing new
is invented for propagation.

The credential-to-config mapping is *derived*, not hand-maintained: every
pydantic settings field declares the environment variable it reads through
``validation_alias``, and a catalog key is by definition that same variable
name. A hand-written table would silently rot the first time a field is
renamed; this cannot.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from typing import TYPE_CHECKING, Any

from app.config.credential_catalog import CATALOG
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.config.config_holder import ConfigHolder
    from app.config.settings import AppConfig
    from app.infrastructure.persistence.credential_store import CredentialStore

logger = get_logger(__name__)

__all__ = ["CredentialConfigReloader", "find_credential_fields"]


def find_credential_fields(cfg: AppConfig) -> dict[str, list[tuple[str, str]]]:
    """Map each catalog key to the ``(section, field)`` pairs that read it.

    Walks the config sections and matches pydantic ``validation_alias`` values
    against catalog keys. One credential may feed several fields.
    """
    mapping: dict[str, list[tuple[str, str]]] = {}
    for section_name in dir(cfg):
        if section_name.startswith("_"):
            continue
        section = getattr(cfg, section_name, None)
        model_fields = getattr(section, "model_fields", None)
        if not isinstance(model_fields, dict):
            continue
        for field_name, field in model_fields.items():
            alias = getattr(field, "validation_alias", None)
            if isinstance(alias, str) and alias in CATALOG:
                mapping.setdefault(alias, []).append((section_name, field_name))
    return mapping


class CredentialConfigReloader:
    """Refresh the live config from stored credentials."""

    def __init__(self, holder: ConfigHolder, store: CredentialStore, *, owner_id: int) -> None:
        self._holder = holder
        self._store = store
        self._owner_id = owner_id

    async def refresh(self) -> bool:
        """Apply stored credentials to the config. Returns True if anything changed."""
        old_cfg = self._holder.cfg
        mapping = find_credential_fields(old_cfg)
        if not mapping:
            return False

        section_updates: dict[str, dict[str, Any]] = {}
        rotated: list[str] = []

        for key, targets in mapping.items():
            value = await self._store.resolve(key, user_id=self._owner_id)
            if not value:
                continue
            for section_name, field_name in targets:
                section = getattr(old_cfg, section_name, None)
                if section is None or getattr(section, field_name, None) == value:
                    continue
                section_updates.setdefault(section_name, {})[field_name] = value
                if key not in rotated:
                    rotated.append(key)

        if not section_updates:
            return False

        app_updates = {
            name: getattr(old_cfg, name).model_copy(update=updates)
            for name, updates in section_updates.items()
        }
        self._holder.swap(dc_replace(old_cfg, **app_updates))
        # Log which credentials rotated, never their values.
        logger.info("credentials_hot_reloaded", extra={"credentials": rotated})
        return True
