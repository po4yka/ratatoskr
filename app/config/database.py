from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from app.config._secret_marker import SECRET_MARKER
from app.config.validation_helpers import parse_positive_int


class DatabaseConfig(BaseModel):
    """Database operation limits and timeouts configuration."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    dsn: str = Field(
        default="",
        validation_alias="DATABASE_URL",
        description="SQLAlchemy asyncpg PostgreSQL DSN",
        json_schema_extra=SECRET_MARKER,
    )
    pool_size: int = Field(
        default=8,
        validation_alias="DATABASE_POOL_SIZE",
        description="SQLAlchemy async connection pool size",
    )
    max_overflow: int = Field(
        default=4,
        validation_alias="DATABASE_MAX_OVERFLOW",
        description="SQLAlchemy async connection pool overflow",
    )
    pool_recycle_seconds: int = Field(
        default=900,
        validation_alias="DATABASE_POOL_RECYCLE_SECONDS",
        description="Seconds before SQLAlchemy recycles pooled connections",
    )
    operation_timeout: float = Field(
        default=30.0,
        validation_alias="DB_OPERATION_TIMEOUT",
        description="Database operation timeout in seconds",
    )
    max_retries: int = Field(
        default=3,
        validation_alias="DB_MAX_RETRIES",
        description="Maximum retries for transient database errors",
    )
    json_max_size: int = Field(
        default=10_000_000,
        validation_alias="DB_JSON_MAX_SIZE",
        description="Maximum JSON payload size in bytes (10MB)",
    )
    json_max_depth: int = Field(
        default=20,
        validation_alias="DB_JSON_MAX_DEPTH",
        description="Maximum JSON nesting depth",
    )
    json_max_array_length: int = Field(
        default=10_000,
        validation_alias="DB_JSON_MAX_ARRAY_LENGTH",
        description="Maximum JSON array length",
    )
    json_max_dict_keys: int = Field(
        default=1_000,
        validation_alias="DB_JSON_MAX_DICT_KEYS",
        description="Maximum JSON dictionary keys",
    )

    @model_validator(mode="after")
    def _derive_and_validate_dsn(self) -> DatabaseConfig:
        dsn = self.dsn.strip()
        if not dsn:
            password = os.getenv("POSTGRES_PASSWORD", "").strip()
            if password:
                dsn = f"postgresql+asyncpg://ratatoskr_app:{password}@postgres:5432/ratatoskr"
                object.__setattr__(self, "dsn", dsn)
        if not self.dsn.startswith("postgresql+asyncpg://"):
            msg = "DATABASE_URL must use postgresql+asyncpg://..."
            raise ValueError(msg)
        return self

    @field_validator("operation_timeout", mode="before")
    @classmethod
    def _validate_timeout(cls, value: Any) -> float:
        if value in (None, ""):
            return 30.0
        try:
            parsed = float(str(value))
        except ValueError as exc:
            msg = "Database operation timeout must be a valid number"
            raise ValueError(msg) from exc
        if parsed <= 0:
            msg = "Database operation timeout must be positive"
            raise ValueError(msg)
        if parsed > 3600:
            msg = "Database operation timeout must be 3600 seconds or less"
            raise ValueError(msg)
        return parsed

    @field_validator(
        "pool_size",
        "max_overflow",
        "pool_recycle_seconds",
        "max_retries",
        "json_max_size",
        "json_max_depth",
        "json_max_array_length",
        "json_max_dict_keys",
        mode="before",
    )
    @classmethod
    def _validate_positive_int(cls, value: Any, info: ValidationInfo) -> int:
        default = cls.model_fields[info.field_name].default
        return parse_positive_int(value, field_name=info.field_name, default=default)
