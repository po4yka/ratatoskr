"""Lightweight content classification for model routing.

Classifies extracted content into tiers (DEFAULT, TECHNICAL, SOCIOPOLITICAL)
using URL domain signals and keyword heuristics. No LLM call required.

When the heuristic produces a tie (``tech_weight == socio_weight >= 1``)
an optional :class:`LLMTierClassifier` can be consulted to resolve the
ambiguity via a cheap single-label LLM call. The classifier is opt-in
(disabled by default) and fails soft to ``None`` on any error so the
caller falls through to ``DEFAULT``.
"""

from __future__ import annotations

import enum
import hashlib
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.adapters.llm.protocol import LLMClientProtocol

logger = get_logger(__name__)

_SCAN_CHARS = 3000
_MIN_KEYWORD_MATCHES = 3


class ContentTier(enum.Enum):
    """Content category for model routing decisions."""

    DEFAULT = "default"
    TECHNICAL = "technical"
    SOCIOPOLITICAL = "sociopolitical"
    QUICK = "quick"


# ---------------------------------------------------------------------------
# Domain-based signals
# ---------------------------------------------------------------------------

_TECHNICAL_DOMAINS: frozenset[str] = frozenset(
    {
        "arxiv.org",
        "ieee.org",
        "acm.org",
        "dl.acm.org",
        "nature.com",
        "science.org",
        "pnas.org",
        "springer.com",
        "link.springer.com",
        "sciencedirect.com",
        "researchgate.net",
        "pubmed.ncbi.nlm.nih.gov",
        "biorxiv.org",
        "medrxiv.org",
        "github.com",
        "stackoverflow.com",
        "proceedings.neurips.cc",
        "openreview.net",
        "aclanthology.org",
        "journals.aps.org",
        "iopscience.iop.org",
        "docs.python.org",
        "developer.mozilla.org",
        "learn.microsoft.com",
        "cloud.google.com",
        "docs.aws.amazon.com",
    }
)

_SOCIOPOLITICAL_DOMAINS: frozenset[str] = frozenset(
    {
        "politico.com",
        "foreignaffairs.com",
        "foreignpolicy.com",
        "theatlantic.com",
        "newyorker.com",
        "economist.com",
        "nytimes.com",
        "washingtonpost.com",
        "theguardian.com",
        "bbc.com",
        "bbc.co.uk",
        "aljazeera.com",
        "history.com",
        "smithsonianmag.com",
        "brookings.edu",
        "cfr.org",
        "rand.org",
        "lawfaremedia.org",
        "justsecurity.org",
        "cnn.com",
        "reuters.com",
        "apnews.com",
    }
)

# ---------------------------------------------------------------------------
# Keyword-based signals
# ---------------------------------------------------------------------------

_TECHNICAL_KEYWORDS: tuple[str, ...] = (
    "abstract",
    "methodology",
    "doi:",
    "et al.",
    "arxiv",
    "algorithm",
    "implementation",
    "benchmark",
    "theorem",
    "proof",
    "equation",
    "dataset",
    "neural network",
    "regression",
    "hypothesis",
    "p-value",
    "statistically significant",
    "architecture",
    "latency",
    "throughput",
    "complexity",
    "compiler",
    "runtime",
    "kernel",
    "protocol",
    "specification",
    "api",
    "framework",
    "repository",
    "pull request",
    "container",
    "kubernetes",
    "microservice",
    "machine learning",
    "deep learning",
    "transformer",
    "gradient",
    "backpropagation",
    "optimization",
)

_SOCIOPOLITICAL_KEYWORDS: tuple[str, ...] = (
    "geopolitical",
    "diplomacy",
    "sanctions",
    "legislation",
    "congress",
    "parliament",
    "election",
    "democracy",
    "authoritarian",
    "sovereignty",
    "treaty",
    "foreign policy",
    "colonialism",
    "imperialism",
    "civil rights",
    "social justice",
    "inequality",
    "discrimination",
    "immigration",
    "refugee",
    "warfare",
    "military",
    "nuclear",
    "nato",
    "editorial",
    "commentary",
    "historical",
    "century",
    "era",
    "civilization",
    "revolution",
    "independence",
    "political",
    "government",
    "constitution",
    "amendment",
    "bipartisan",
    "liberal",
    "conservative",
)


def _extract_domain(url: str) -> str | None:
    """Extract the registrable domain from a URL."""
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return None
        # Strip leading www.
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname.lower()
    except Exception:
        return None


def _domain_signal(url: str | None) -> ContentTier | None:
    """Return a tier hint based on URL domain, or None if unknown."""
    if not url:
        return None
    domain = _extract_domain(url)
    if not domain:
        return None
    # Check exact match first, then parent domain (e.g. sub.nature.com -> nature.com)
    for known in _TECHNICAL_DOMAINS:
        if domain == known or domain.endswith("." + known):
            return ContentTier.TECHNICAL
    for known in _SOCIOPOLITICAL_DOMAINS:
        if domain == known or domain.endswith("." + known):
            return ContentTier.SOCIOPOLITICAL
    return None


def _keyword_score(text_lower: str, keywords: tuple[str, ...]) -> int:
    """Count how many keywords appear in the text."""
    return sum(1 for kw in keywords if kw in text_lower)


def _content_signal(content_text: str) -> tuple[int, int]:
    """Return (technical_score, sociopolitical_score) from keyword analysis."""
    sample = content_text[:_SCAN_CHARS].lower()
    tech = _keyword_score(sample, _TECHNICAL_KEYWORDS)
    socio = _keyword_score(sample, _SOCIOPOLITICAL_KEYWORDS)
    return tech, socio


def _heuristic_weights(
    content_text: str, *, url: str | None
) -> tuple[ContentTier, int, int, ContentTier | None, int, int]:
    """Return ``(tier, tech_weight, socio_weight, domain_tier, tech_score, socio_score)``.

    Pure function — no logging. Shared by sync ``classify_content`` and
    async ``classify_content_async``.
    """
    tech_weight = 0
    socio_weight = 0

    domain_tier = _domain_signal(url)
    if domain_tier == ContentTier.TECHNICAL:
        tech_weight += 2
    elif domain_tier == ContentTier.SOCIOPOLITICAL:
        socio_weight += 2

    tech_score, socio_score = _content_signal(content_text)
    if tech_score >= _MIN_KEYWORD_MATCHES:
        tech_weight += 1
    if socio_score >= _MIN_KEYWORD_MATCHES:
        socio_weight += 1

    if tech_weight >= 2:
        tier = ContentTier.TECHNICAL
    elif socio_weight >= 2 and tech_weight < 2:
        tier = ContentTier.SOCIOPOLITICAL
    elif tech_weight >= 1 and socio_weight == 0:
        tier = ContentTier.TECHNICAL
    elif socio_weight >= 1 and tech_weight == 0:
        tier = ContentTier.SOCIOPOLITICAL
    else:
        tier = ContentTier.DEFAULT

    return tier, tech_weight, socio_weight, domain_tier, tech_score, socio_score


def _is_tie(tier: ContentTier, tech_weight: int, socio_weight: int) -> bool:
    """A tie is: heuristic returned DEFAULT yet both sides scored >= 1."""
    return tier is ContentTier.DEFAULT and tech_weight >= 1 and socio_weight >= 1


def _log_classification(
    tier: ContentTier,
    *,
    url: str | None,
    domain_tier: ContentTier | None,
    tech_score: int,
    socio_score: int,
    tech_weight: int,
    socio_weight: int,
    resolved_by_llm: bool = False,
) -> None:
    domain = _extract_domain(url) if url else None
    logger.info(
        "content_tier_classified",
        extra={
            "tier": tier.value,
            "domain_signal": domain_tier.value if domain_tier else None,
            "technical_score": tech_score,
            "sociopolitical_score": socio_score,
            "tech_weight": tech_weight,
            "socio_weight": socio_weight,
            "url_domain": domain,
            "resolved_by_llm": resolved_by_llm,
        },
    )


def classify_content(
    content_text: str,
    *,
    url: str | None = None,
) -> ContentTier:
    """Classify content into a tier for model routing.

    Uses a weighted scoring approach:
    - Domain signal (from URL): weight 2
    - Keyword signal (from content): weight 1 per ``_MIN_KEYWORD_MATCHES`` hits

    TECHNICAL wins ties over SOCIOPOLITICAL.
    Returns DEFAULT when signals are insufficient.
    """
    tier, tech_w, socio_w, domain_tier, tech_score, socio_score = _heuristic_weights(
        content_text, url=url
    )
    _log_classification(
        tier,
        url=url,
        domain_tier=domain_tier,
        tech_score=tech_score,
        socio_score=socio_score,
        tech_weight=tech_w,
        socio_weight=socio_w,
    )
    return tier


async def classify_content_async(
    content_text: str,
    *,
    url: str | None = None,
    llm_classifier: LLMTierClassifier | None = None,
) -> ContentTier:
    """Async variant that consults *llm_classifier* on heuristic ties.

    Behaviour matches :func:`classify_content` for unambiguous inputs.
    When the heuristic produces a tie (``tech_weight == socio_weight >= 1``
    falling through to DEFAULT) and an enabled classifier is provided,
    one cheap LLM call is issued to resolve the tier. Any failure or
    disabled classifier falls through to DEFAULT.
    """
    tier, tech_w, socio_w, domain_tier, tech_score, socio_score = _heuristic_weights(
        content_text, url=url
    )

    resolved_by_llm = False
    if llm_classifier is not None and _is_tie(tier, tech_w, socio_w):
        llm_tier = await llm_classifier.resolve_tie(content_text, url=url)
        if llm_tier is not None:
            tier = llm_tier
            resolved_by_llm = True

    _log_classification(
        tier,
        url=url,
        domain_tier=domain_tier,
        tech_score=tech_score,
        socio_score=socio_score,
        tech_weight=tech_w,
        socio_weight=socio_w,
        resolved_by_llm=resolved_by_llm,
    )
    return tier


# ---------------------------------------------------------------------------
# Optional LLM tier classifier (tie-break only)
# ---------------------------------------------------------------------------

_LABEL_TO_TIER: dict[str, ContentTier] = {
    "technical": ContentTier.TECHNICAL,
    "sociopolitical": ContentTier.SOCIOPOLITICAL,
    "default": ContentTier.DEFAULT,
}

_CACHE_TTL_ENTRIES = 256  # in-process cap; cheap LRU-ish trimming


class LLMTierClassifier:
    """Resolve heuristic tier ties via a single cheap LLM call.

    The classifier issues at most one short completion per resolved tie
    and caches the result keyed on the URL (when present) or a sha256 of
    the leading content slice. It is disabled by default; callers must
    pass ``enabled=True`` (typically from configuration).

    All failure modes — disabled, empty input, LLM error, malformed
    label — return ``None`` so the caller can fall through to
    ``ContentTier.DEFAULT`` without raising.
    """

    def __init__(
        self,
        *,
        client: LLMClientProtocol,
        model: str,
        enabled: bool = False,
        max_tokens: int = 8,
    ) -> None:
        self._client = client
        self._model = model
        self._enabled = enabled
        self._max_tokens = max_tokens
        self._cache: dict[str, ContentTier] = {}

    async def resolve_tie(self, content_text: str, *, url: str | None) -> ContentTier | None:
        if not self._enabled:
            return None
        if not content_text or not content_text.strip():
            return None

        cache_key = self._cache_key(content_text, url)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        prompt = (
            "Classify the article below into exactly one tier and reply "
            "with that single word and nothing else. "
            "Allowed tiers: technical, sociopolitical, default.\n\n"
            f"Article (first 512 chars):\n{content_text[:512]}"
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a content router. Reply with exactly one of: "
                    "technical, sociopolitical, default."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        try:
            result = await self._client.chat(
                messages,
                temperature=0.0,
                max_tokens=self._max_tokens,
                model_override=self._model,
            )
        except Exception as exc:
            logger.warning(
                "llm_tier_classifier_call_failed",
                extra={"error": str(exc), "url_domain": _extract_domain(url)},
            )
            return None

        if result.status is not CallStatus.OK or not result.response_text:
            logger.info(
                "llm_tier_classifier_non_ok",
                extra={
                    "status": result.status.value if result.status else None,
                    "url_domain": _extract_domain(url),
                },
            )
            return None

        tier = _parse_tier_label(result.response_text)
        if tier is None:
            logger.info(
                "llm_tier_classifier_unparseable",
                extra={
                    "reply_length": len(result.response_text),
                    "url_domain": _extract_domain(url),
                },
            )
            return None

        # Bounded cap to avoid unbounded memory in long-running workers.
        if len(self._cache) >= _CACHE_TTL_ENTRIES:
            self._cache.clear()
        self._cache[cache_key] = tier

        logger.info(
            "llm_tier_classifier_resolved",
            extra={
                "tier": tier.value,
                "url_domain": _extract_domain(url),
            },
        )
        return tier

    @staticmethod
    def _cache_key(content_text: str, url: str | None) -> str:
        if url:
            return f"url::{url}"
        digest = hashlib.sha256(content_text[:512].encode("utf-8")).hexdigest()
        return f"hash::{digest}"


def _parse_tier_label(reply_text: str) -> ContentTier | None:
    normalized = reply_text.strip().lower().rstrip(".!?")
    # Direct match first.
    if normalized in _LABEL_TO_TIER:
        return _LABEL_TO_TIER[normalized]
    # Substring match: scan for any allowed label as a whole word.
    for label, tier in _LABEL_TO_TIER.items():
        if label in normalized.split():
            return tier
    # Last resort: scan a 'tier: <label>' prefix or trailing punctuation.
    for label, tier in _LABEL_TO_TIER.items():
        if label in normalized:
            return tier
    return None
