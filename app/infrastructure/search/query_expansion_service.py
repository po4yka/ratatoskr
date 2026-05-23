"""Service for expanding search queries with synonyms and related terms."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from app.core.logging_utils import get_logger

logger = get_logger(__name__)


# Common synonyms and expansions for technical/general terms
SYNONYM_MAP = {
    # Technology terms
    "ai": ["artificial intelligence", "machine learning", "ml", "neural network"],
    "ml": ["machine learning", "ai", "artificial intelligence"],
    "api": ["application programming interface", "endpoint", "service"],
    "db": ["database", "data store", "storage"],
    "ui": ["user interface", "interface", "frontend"],
    "ux": ["user experience", "usability"],
    # Programming terms
    "python": ["py", "programming", "code"],
    "javascript": ["js", "ecmascript"],
    "typescript": ["ts"],
    # General terms
    "tutorial": ["guide", "how-to", "walkthrough", "introduction"],
    "guide": ["tutorial", "how-to", "documentation"],
    "optimization": ["performance", "speed", "efficiency"],
    "security": ["safety", "protection", "vulnerability"],
    "error": ["bug", "issue", "problem", "exception"],
    "development": ["dev", "programming", "coding"],
    # Russian terms (Cyrillic)
    "разработка": ["программирование", "dev", "development"],
    "обучение": ["учёба", "туториал", "гайд"],
}


class ExpandedQuery(BaseModel):
    """Expanded query with original and additional terms."""

    model_config = ConfigDict(frozen=True)

    original: str
    expanded_terms: list[str] = Field(default_factory=list)
    weight_map: dict[str, float] = Field(default_factory=dict)  # Term to weight mapping


class QueryExpansionService:
    """Expands search queries with synonyms and related terms."""

    def __init__(
        self,
        *,
        max_expansions: int = 5,
        use_synonyms: bool = True,
        synonym_map: dict[str, list[str]] | None = None,
    ) -> None:
        """Initialize query expansion service.

        Args:
            max_expansions: Maximum number of expansion terms to add
            use_synonyms: Whether to use synonym expansion
            synonym_map: Custom synonym mapping (uses default if None)
        """
        self._max_expansions = max_expansions
        self._use_synonyms = use_synonyms
        self._synonym_map = synonym_map or SYNONYM_MAP

    def expand_query(self, query: str, *, language: str | None = None) -> ExpandedQuery:
        """Expand query with synonyms and related terms.

        Args:
            query: Original search query
            language: Optional language hint for language-specific expansions

        Returns:
            ExpandedQuery with original and expanded terms
        """
        if not query or not query.strip():
            return ExpandedQuery(original=query, expanded_terms=[], weight_map={})

        query = query.strip().lower()

        # Extract key terms from query
        terms = self._extract_key_terms(query)

        # Generate expansions
        expanded_terms = []
        weight_map = {query: 1.0}  # Original query gets highest weight

        if self._use_synonyms:
            for term in terms:
                synonyms = self._find_synonyms(term)
                for synonym in synonyms[: self._max_expansions]:
                    if synonym not in expanded_terms and synonym != term:
                        expanded_terms.append(synonym)
                        # Synonyms get lower weight than original
                        weight_map[synonym] = 0.7

        logger.debug(
            "query_expanded",
            extra={
                "original": query,
                "expanded_count": len(expanded_terms),
                "terms": expanded_terms[:5],
            },
        )

        return ExpandedQuery(
            original=query,
            expanded_terms=expanded_terms,
            weight_map=weight_map,
        )

    def expand_for_fts(self, query: str) -> str:
        """Expand query for keyword search.

        Args:
            query: Original query

        Returns:
            Expanded keyword query string
        """
        expanded = self.expand_query(query)

        # Combine original and top expansions with OR
        all_terms = [expanded.original]
        all_terms.extend(expanded.expanded_terms[: self._max_expansions])

        # Keep the quoted OR form expected by the keyword-search adapter.
        return " OR ".join(f'"{term}"' for term in all_terms if term)

    def _extract_key_terms(self, query: str) -> list[str]:
        """Extract key searchable terms from query.

        Args:
            query: Search query

        Returns:
            List of key terms
        """
        # Split on common separators and whitespace
        terms = re.split(r"[\s,;]+", query.lower())

        # Filter out very short terms and common stop words
        stop_words = {
            "a",
            "an",
            "the",
            "and",
            "or",
            "but",
            "in",
            "on",
            "at",
            "to",
            "for",
            "of",
            "with",
            "by",
            "from",
            "about",
            "как",
            "что",
            "это",
            "для",
        }

        return [
            term.strip()
            for term in terms
            if len(term.strip()) > 2 and term.strip() not in stop_words
        ]

    def _find_synonyms(self, term: str) -> list[str]:
        """Find synonyms for a term.

        Args:
            term: Term to find synonyms for

        Returns:
            List of synonyms
        """
        term_lower = term.lower()

        # Check custom synonym map
        if term_lower in self._synonym_map:
            return self._synonym_map[term_lower].copy()

        # Try partial matches (for compound terms)
        synonyms = []
        for key, values in self._synonym_map.items():
            if key in term_lower or term_lower in key:
                synonyms.extend(values)

        return synonyms[: self._max_expansions]

    def add_custom_synonym(self, term: str, synonyms: list[str]) -> None:
        """Add custom synonym mapping.

        Args:
            term: Term to add synonyms for
            synonyms: List of synonym terms
        """
        term_lower = term.lower()
        if term_lower in self._synonym_map:
            self._synonym_map[term_lower].extend(
                [s for s in synonyms if s not in self._synonym_map[term_lower]]
            )
        else:
            self._synonym_map[term_lower] = synonyms
