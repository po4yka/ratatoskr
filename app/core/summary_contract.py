"""Runtime validation functions for the summary JSON contract (field checks, shaping, schema)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from app.core.summary_contract_impl.common import SummaryJSON
from app.core.summary_contract_impl.contract import (
    cap_text,
    extract_keywords_tfidf,
    get_summary_json_schema,
    normalize_whitespace,
    validate_and_shape_summary as _validate_python,
)
from app.types.summary_types import (
    Entities,
    KeyStat,
    Metadata,
    Readability,
    SemanticChunk,
    SummaryDict,
)

SummaryContractId = Literal["default"]
SummaryPromptLoader = Callable[..., str]
SummarySchemaLoader = Callable[[], dict[str, Any]]
SummaryCompatibilityMapper = Callable[[SummaryJSON], SummaryJSON]


@dataclass(frozen=True, slots=True)
class SummaryContractDescriptor:
    """Descriptor for a summary JSON contract variant."""

    contract_id: SummaryContractId
    schema_name: str
    supported_languages: tuple[str, ...]
    schema_loader: SummarySchemaLoader
    prompt_loader: SummaryPromptLoader
    compatibility_mapper: SummaryCompatibilityMapper

    def response_format(self, mode: str | None = None) -> dict[str, Any]:
        """Build provider response-format configuration for this contract."""
        if mode == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": self.schema_name,
                    "schema": self.schema_loader(),
                    "strict": True,
                },
            }
        return {"type": "json_object"}

    def repair_response_format(self) -> dict[str, Any]:
        """Build repair-attempt response-format configuration for this contract."""
        return {"type": "json_object"}


def _load_default_summary_prompt(lang: str = "en", **kwargs: Any) -> str:
    from app.prompts.manager import get_prompt_manager

    return get_prompt_manager().get_system_prompt(lang, **kwargs)


DEFAULT_SUMMARY_CONTRACT_ID: SummaryContractId = "default"
DEFAULT_SUMMARY_CONTRACT_DESCRIPTOR = SummaryContractDescriptor(
    contract_id=DEFAULT_SUMMARY_CONTRACT_ID,
    schema_name="summary_schema",
    supported_languages=("en", "ru"),
    schema_loader=get_summary_json_schema,
    prompt_loader=_load_default_summary_prompt,
    compatibility_mapper=lambda payload: _validate_python(payload),
)


def get_summary_contract_descriptor(
    contract_id: SummaryContractId = DEFAULT_SUMMARY_CONTRACT_ID,
) -> SummaryContractDescriptor:
    """Return the summary contract descriptor for the given contract ID."""
    return DEFAULT_SUMMARY_CONTRACT_DESCRIPTOR


def validate_and_shape_summary(payload: SummaryJSON) -> SummaryJSON:
    """Validate and shape summary payload.

    Runs the Python validation and shaping pipeline defined in
    ``summary_contract_impl.contract``.
    """

    return DEFAULT_SUMMARY_CONTRACT_DESCRIPTOR.compatibility_mapper(payload)


# Re-export typed versions for stricter typing
__all__ = [
    "DEFAULT_SUMMARY_CONTRACT_DESCRIPTOR",
    "DEFAULT_SUMMARY_CONTRACT_ID",
    "Entities",
    "KeyStat",
    "Metadata",
    "Readability",
    "SemanticChunk",
    "SummaryContractDescriptor",
    "SummaryContractId",
    "SummaryDict",
    "SummaryJSON",
    "cap_text",
    "extract_keywords_tfidf",
    "get_summary_contract_descriptor",
    "get_summary_json_schema",
    "normalize_whitespace",
    "validate_and_shape_summary",
]
