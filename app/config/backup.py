"""Backup encryption and safety-limit configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from ._secret_marker import SECRET_MARKER


class BackupConfig(BaseModel):
    """Encryption key, feature flag, and ZIP safety limits for the backup subsystem."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    encryption_key: SecretStr | None = Field(
        default=None,
        validation_alias="BACKUP_ENCRYPTION_KEY",
        description=(
            "Fernet key (44-char url-safe base64). "
            'Generate with: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ),
        json_schema_extra=SECRET_MARKER,
    )
    encryption_enabled: bool | None = Field(
        default=None,
        validation_alias="BACKUP_ENCRYPTION_ENABLED",
        description=(
            "Explicit on/off override. Omit to auto-derive: "
            "True when encryption_key is set, False otherwise."
        ),
    )

    max_restore_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1024,
        validation_alias="BACKUP_RESTORE_MAX_UPLOAD_BYTES",
        description="Maximum upload size in bytes for the restore endpoint (default 100 MB).",
    )
    max_zip_entries: int = Field(
        default=100,
        ge=1,
        validation_alias="BACKUP_MAX_ZIP_ENTRIES",
        description="Maximum number of entries allowed in a restore archive.",
    )
    max_compressed_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
        validation_alias="BACKUP_MAX_COMPRESSED_BYTES",
        description="Maximum total compressed size of all ZIP entries (default 100 MB).",
    )
    max_decompressed_bytes: int = Field(
        default=500 * 1024 * 1024,
        ge=1,
        validation_alias="BACKUP_MAX_DECOMPRESSED_BYTES",
        description="Maximum total uncompressed size of all ZIP entries (default 500 MB).",
    )
    max_compression_ratio: float = Field(
        default=100.0,
        ge=1.0,
        validation_alias="BACKUP_MAX_COMPRESSION_RATIO",
        description="Maximum per-entry compression ratio — zip bomb guard (default 100).",
    )

    @model_validator(mode="after")
    def _key_required_when_explicitly_enabled(self) -> BackupConfig:
        if self.encryption_enabled is True and self.encryption_key is None:
            raise ValueError(
                "BACKUP_ENCRYPTION_ENABLED=true requires BACKUP_ENCRYPTION_KEY to be set."
            )
        return self

    @property
    def is_encryption_enabled(self) -> bool:
        """True if backups should be encrypted.

        Auto-derives from key presence when BACKUP_ENCRYPTION_ENABLED is unset.
        """
        if self.encryption_enabled is not None:
            return self.encryption_enabled
        return self.encryption_key is not None


def load_backup_config() -> BackupConfig:
    """Return BackupConfig from the current application settings (lazy, cached via load_config)."""
    from app.config.settings import load_config

    return load_config(allow_stub_telegram=True).backup
