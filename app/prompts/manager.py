"""Unified prompt management for summarization.

This module provides a single source of truth for loading, caching, and validating
system prompts and few-shot examples.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.prompts.file_cache import clear_prompt_file_cache

if TYPE_CHECKING:
    from app.core.summary_contract import SummaryContractId

logger = get_logger(__name__)

# Prompt directory layout
_PROMPT_DIR = Path(__file__).parent
_EXAMPLES_DIR = _PROMPT_DIR / "examples"

# Supported languages
SUPPORTED_LANGUAGES = frozenset({"en", "ru"})

# Required fields that must be mentioned in the prompt
REQUIRED_PROMPT_FIELDS = frozenset(
    {
        "summary_250",
        "summary_1000",
        "tldr",
        "key_ideas",
        "topic_tags",
        "entities",
        "estimated_reading_time_min",
        "key_stats",
        "readability",
        "source_type",
        "temporal_freshness",
    }
)

# Prompt version pattern for tracking
_VERSION_PATTERN = re.compile(r"^#\s*@version:\s*(.+)$", re.MULTILINE)
_FIELDS_PATTERN = re.compile(r"^#\s*@fields:\s*(.+)$", re.MULTILINE)


class PromptValidationError(Exception):
    """Raised when a prompt fails validation."""


class PromptManager:
    """Unified manager for loading and caching system prompts.

    Features:
    - LRU cache with file hash validation for freshness
    - Language validation (en/ru only)
    - Content validation (checks required fields mentioned)
    - Prompt version tracking
    - Few-shot example injection
    """

    def __init__(
        self,
        prompt_dir: Path | None = None,
        examples_dir: Path | None = None,
        validate_on_load: bool = True,
        cache_size: int = 8,
    ):
        self.prompt_dir = prompt_dir or _PROMPT_DIR
        self.examples_dir = examples_dir or _EXAMPLES_DIR
        self.validate_on_load = validate_on_load
        self._cache_size = cache_size
        # key -> (content, mtime_ns); mtime is a cheap stat-based freshness check
        # that avoids re-reading and hashing the whole file on every lookup.
        self._prompt_cache: dict[str, tuple[str, int]] = {}
        self._example_cache: dict[str, list[dict[str, Any]]] = {}

    def get_system_prompt(
        self,
        lang: str = "en",
        *,
        include_examples: bool = True,
        num_examples: int = 2,
        example_types: list[str] | None = None,
    ) -> str:
        """Get the system prompt for a given language.

        Args:
            lang: Language code ('en' or 'ru')
            include_examples: Whether to include few-shot examples
            num_examples: Number of examples to include (1-3 recommended)
            example_types: Specific example types to include (e.g., ['news', 'technical'])

        Returns:
            Complete system prompt with optional examples

        Raises:
            FileNotFoundError: If prompt file not found
        """
        lang = self._normalize_language(lang)
        prompt = self._load_prompt(lang)

        if include_examples:
            examples = self._get_examples(lang, num_examples, example_types)
            if examples:
                prompt = self._inject_examples(prompt, examples, lang)

        return prompt

    def get_contract_system_prompt(
        self,
        contract_id: SummaryContractId = "default",
        lang: str = "en",
        *,
        include_examples: bool = True,
        num_examples: int = 2,
        example_types: list[str] | None = None,
    ) -> str:
        """Get a paired system prompt for a summary contract."""
        from app.core.summary_contract import get_summary_contract_descriptor

        descriptor = get_summary_contract_descriptor(contract_id)
        normalized_lang = self._normalize_language(lang)
        if normalized_lang not in descriptor.supported_languages:
            logger.warning(
                "summary_contract_language_fallback",
                extra={
                    "contract_id": descriptor.contract_id,
                    "requested": normalized_lang,
                    "fallback": "en",
                },
            )
            normalized_lang = "en"
        return self.get_system_prompt(
            normalized_lang,
            include_examples=include_examples,
            num_examples=num_examples,
            example_types=example_types,
        )

    def get_prompt_version(self, lang: str = "en") -> str | None:
        """Extract version string from prompt file header.

        Args:
            lang: Language code

        Returns:
            Version string or None if not specified
        """
        lang = self._normalize_language(lang)
        prompt = self._load_prompt(lang)

        match = _VERSION_PATTERN.search(prompt)
        return match.group(1).strip() if match else None

    def get_prompt_fields(self, lang: str = "en") -> list[str]:
        """Extract declared fields from prompt file header.

        Args:
            lang: Language code

        Returns:
            List of declared field names
        """
        lang = self._normalize_language(lang)
        prompt = self._load_prompt(lang)

        match = _FIELDS_PATTERN.search(prompt)
        if match:
            return [f.strip() for f in match.group(1).split(",")]
        return []

    def validate_prompt(self, prompt_text: str, lang: str = "en") -> list[str]:
        """Validate a prompt for required content.

        Args:
            prompt_text: The prompt text to validate
            lang: Language code for context

        Returns:
            List of validation error messages (empty if valid)
        """
        errors: list[str] = []
        _ = lang  # Reserved for future language-specific validation

        # Check for empty or minimal prompt
        if len(prompt_text.strip()) < 100:
            errors.append(f"Prompt too short ({len(prompt_text)} chars)")

        # Check required fields are mentioned
        missing_fields = []
        for field in REQUIRED_PROMPT_FIELDS:
            if field not in prompt_text:
                missing_fields.append(field)

        if missing_fields:
            errors.append(f"Missing required fields: {', '.join(missing_fields)}")

        # Check for JSON-only output instruction
        json_patterns = ["only.*json", "json.*only", "return.*json", "output.*json"]
        has_json_instruction = any(
            re.search(pattern, prompt_text.lower()) for pattern in json_patterns
        )
        if not has_json_instruction:
            errors.append("Prompt should instruct JSON-only output")

        return errors

    def clear_cache(self) -> None:
        """Clear all cached prompts and examples."""
        self._prompt_cache.clear()
        self._example_cache.clear()

    def _normalize_language(self, lang: str) -> str:
        """Normalize and validate language code."""
        lang = lang.lower().strip()

        # Handle common aliases
        lang_map = {
            "english": "en",
            "russian": "ru",
            "rus": "ru",
            "eng": "en",
        }
        lang = lang_map.get(lang, lang)

        if lang not in SUPPORTED_LANGUAGES:
            logger.warning(
                "unsupported_language",
                extra={"requested": lang, "fallback": "en"},
            )
            return "en"

        return lang

    def _load_prompt(self, lang: str) -> str:
        """Load a prompt file with caching and validation."""
        fname = f"summary_system_{lang}.txt"
        path = self.prompt_dir / fname

        # Check cache
        cache_key = f"{lang}:{path}"
        current_mtime = self._file_mtime(path)
        if cache_key in self._prompt_cache:
            cached_content, cached_mtime = self._prompt_cache[cache_key]
            # Verify file hasn't changed (cheap stat, no full read+hash per call)
            if current_mtime == cached_mtime and current_mtime != 0:
                return cached_content

        # Load from file
        try:
            content = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            logger.error("prompt_file_not_found", extra={"path": str(path)})
            raise
        except Exception as e:
            logger.error(
                "prompt_load_error",
                extra={"path": str(path), "error": str(e)},
            )
            raise

        # Validate if enabled
        if self.validate_on_load:
            errors = self.validate_prompt(content, lang)
            if errors:
                logger.warning(
                    "prompt_validation_warnings",
                    extra={"path": str(path), "errors": errors},
                )
                # Log warnings but don't fail - allow graceful degradation

        # Cache with mtime
        self._prompt_cache[cache_key] = (content, current_mtime)

        # Evict old entries if cache too large
        while len(self._prompt_cache) > self._cache_size:
            oldest_key = next(iter(self._prompt_cache))
            del self._prompt_cache[oldest_key]

        logger.debug(
            "prompt_loaded",
            extra={
                "path": str(path),
                "length": len(content),
                "mtime_ns": current_mtime,
            },
        )

        return content

    def _get_examples(
        self,
        lang: str,
        num_examples: int,
        example_types: list[str] | None,
    ) -> list[dict[str, Any]]:
        """Load few-shot examples for a language."""
        if lang in self._example_cache:
            examples = self._example_cache[lang]
        else:
            examples = self._load_examples(lang)
            self._example_cache[lang] = examples

        if not examples:
            return []

        # Filter by type if specified
        if example_types:
            examples = [e for e in examples if e.get("content_type") in example_types]

        # Deterministic selection: examples are loaded in a stable (filename) order,
        # so taking the first N keeps the prompt identical across calls. This keeps
        # the provider-side prompt-cache fingerprint stable (vs. random.sample).
        return examples[:num_examples]

    def _load_examples(self, lang: str) -> list[dict[str, Any]]:
        """Load example files for a language."""
        if not self.examples_dir.exists():
            logger.debug("examples_dir_not_found", extra={"path": str(self.examples_dir)})
            return []

        examples: list[dict[str, Any]] = []
        pattern = f"*_{lang}.json" if lang != "en" else "*.json"

        # Sort by filename for a deterministic, reproducible load order.
        for path in sorted(self.examples_dir.glob(pattern), key=lambda p: p.name):
            # Skip non-target language files for English
            if lang == "en" and "_ru.json" in path.name:
                continue

            try:
                with path.open(encoding="utf-8") as f:
                    example = json.load(f)
                    if isinstance(example, dict) and "expected_output" in example:
                        examples.append(example)
            except Exception as e:
                logger.warning(
                    "example_load_failed",
                    extra={"path": str(path), "error": str(e)},
                )

        logger.debug(
            "examples_loaded",
            extra={"lang": lang, "count": len(examples)},
        )

        return examples

    def _inject_examples(
        self,
        prompt: str,
        examples: list[dict[str, Any]],
        lang: str,
    ) -> str:
        """Inject few-shot examples into the prompt."""
        if not examples:
            return prompt

        example_section = self._format_examples_section(examples, lang)
        # Insert examples before the rules section or at the end
        rules_marker = "Rules:" if lang == "en" else "Правила:"
        if rules_marker in prompt:
            idx = prompt.find(rules_marker)
            return prompt[:idx] + example_section + "\n\n" + prompt[idx:]
        return prompt + "\n\n" + example_section

    def _format_examples_section(
        self,
        examples: list[dict[str, Any]],
        lang: str,
    ) -> str:
        """Format examples as a prompt section."""
        header = "ПРИМЕРЫ ПРАВИЛЬНЫХ ОТВЕТОВ:" if lang == "ru" else "EXAMPLE CORRECT OUTPUTS:"

        sections = [header]
        for i, example in enumerate(examples, 1):
            content_type = example.get("content_type", "article")
            expected = example.get("expected_output", {})

            if lang == "ru":
                sections.append(f"\n--- Пример {i} ({content_type}) ---")
            else:
                sections.append(f"\n--- Example {i} ({content_type}) ---")

            # Show a compact version of the expected output
            compact_output = self._compact_example_output(expected)
            sections.append(
                f"```json\n{json.dumps(compact_output, ensure_ascii=False, indent=2)}\n```"
            )

        return "\n".join(sections)

    def _compact_example_output(self, output: dict[str, Any]) -> dict[str, Any]:
        """Create a compact version of an example output for the prompt."""
        # Include key fields to demonstrate format, truncate arrays
        compact = {}

        # Core summary fields (always include)
        for key in ["summary_250", "summary_1000", "tldr"]:
            if key in output:
                compact[key] = output[key]

        # Structured fields (show format with truncation)
        if "key_ideas" in output:
            ideas = output["key_ideas"]
            compact["key_ideas"] = [*ideas[:3], "..."] if len(ideas) > 3 else ideas

        if "topic_tags" in output:
            tags = output["topic_tags"]
            compact["topic_tags"] = [*tags[:4], "..."] if len(tags) > 4 else tags

        if "entities" in output:
            compact["entities"] = output["entities"]

        if "source_type" in output:
            compact["source_type"] = output["source_type"]

        if "temporal_freshness" in output:
            compact["temporal_freshness"] = output["temporal_freshness"]

        if "key_stats" in output:
            stats = output["key_stats"]
            compact["key_stats"] = stats[:2] if len(stats) > 2 else stats

        if "insights" in output and isinstance(output["insights"], dict):
            compact["insights"] = {
                "topic_overview": output["insights"].get("topic_overview", "")[:150] + "...",
                "new_facts": output["insights"].get("new_facts", [])[:2],
                "open_questions": output["insights"].get("open_questions", [])[:2],
            }

        return compact

    @staticmethod
    def _file_mtime(path: Path) -> int:
        """Return the file's modification time in ns for cheap cache invalidation."""
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0


# Module-level singleton for convenience
_default_manager: PromptManager | None = None


def get_prompt_manager() -> PromptManager:
    """Get or create the default PromptManager instance."""
    global _default_manager
    if _default_manager is None:
        _default_manager = PromptManager()
    return _default_manager


def reset_prompt_manager() -> None:
    """Reset module-level caches. Intended for test teardown only."""
    global _default_manager
    _default_manager = None
    clear_prompt_file_cache()
