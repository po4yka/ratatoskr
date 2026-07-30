"""Use case: pick the star list a newly added repository belongs in.

The user has already filed over a thousand repositories by hand, so their own
labelling is the best available training signal. The primary path embeds the new
repository's text and asks which of the user's existing repositories are nearest,
then votes over the lists those neighbours belong to.

The vote is **weighted by similarity**, not a count. A single neighbour at 0.90
similarity is stronger evidence than three at 0.36: a plain count inverts that and
files the repository by whichever topic happens to be most crowded.

When the vote is inconclusive — nothing similar, or a near-tie — an LLM picks from
the same fixed set of lists. When that is also inconclusive the answer is "no
list": the repository is still starred and mirrored, just unfiled. Guessing is
worse than leaving it, because a wrong list is silent and the 32 slots are full.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from app.application.use_cases._tracing import use_case_span
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.application.ports.star_list_suggestion import (
        NeighbourSearchPort,
        StarListClassifierPort,
    )
    from app.core.star_list_suggestion_schema import StarListCandidate, StarListChoice

logger = get_logger(__name__)

SuggestionSource = Literal["knn", "llm", "none"]

_QUERY_README_BUDGET = 2000


@dataclass(frozen=True)
class StarListSuggestion:
    """What to file the repository under, and where the answer came from."""

    list_names: list[str]
    source: SuggestionSource
    confidence: float = 0.0
    reason: str = ""
    # Weighted score of every list the neighbours voted for, best first. Kept for
    # logging and for a caller that wants to show the runners-up.
    scores: dict[str, float] = field(default_factory=dict)


class SuggestStarListsUseCase:
    """Nearest-neighbour vote over the user's own filing, with an LLM fallback."""

    def __init__(
        self,
        *,
        neighbour_search: NeighbourSearchPort,
        classifier: StarListClassifierPort | None = None,
        neighbour_limit: int = 15,
        min_similarity: float = 0.35,
        min_score: float = 1.0,
        dominance_ratio: float = 1.5,
    ) -> None:
        self._neighbour_search = neighbour_search
        self._classifier = classifier
        self._neighbour_limit = neighbour_limit
        self._min_similarity = min_similarity
        self._min_score = min_score
        self._dominance_ratio = dominance_ratio

    async def suggest(
        self,
        *,
        user_id: int,
        repository_id: int,
        full_name: str,
        description: str | None,
        language: str | None,
        topics: list[str],
        readme_excerpt: str | None,
        available_lists: list[StarListCandidate],
        correlation_id: str | None = None,
    ) -> StarListSuggestion:
        """Return the list to file *repository_id* under, or an empty suggestion."""
        with use_case_span("star_lists.suggest"):
            scores = await self._vote(
                user_id=user_id,
                repository_id=repository_id,
                description=description,
                language=language,
                topics=topics,
                readme_excerpt=readme_excerpt,
                available_lists=available_lists,
                correlation_id=correlation_id,
            )
            decided = self._decide(scores)
            if decided is not None:
                name, score = decided
                logger.info(
                    "star_list_suggested_by_knn",
                    extra={"full_name": full_name, "list": name, "score": round(score, 3)},
                )
                return StarListSuggestion(
                    list_names=[name],
                    source="knn",
                    confidence=min(1.0, score / max(self._min_score, 1e-9)),
                    reason="matches repositories already filed under this list",
                    scores=scores,
                )

            choice = await self._classify(
                full_name=full_name,
                description=description,
                language=language,
                topics=topics,
                readme_excerpt=readme_excerpt,
                available_lists=available_lists,
                correlation_id=correlation_id,
            )
            if choice is not None:
                logger.info(
                    "star_list_suggested_by_llm",
                    extra={"full_name": full_name, "list": choice.list_name},
                )
                return StarListSuggestion(
                    list_names=[choice.list_name],
                    source="llm",
                    confidence=float(choice.confidence),
                    reason=str(choice.reason or ""),
                    scores=scores,
                )

        logger.info("star_list_not_suggested", extra={"full_name": full_name})
        return StarListSuggestion(list_names=[], source="none", scores=scores)

    async def _vote(
        self,
        *,
        user_id: int,
        repository_id: int,
        description: str | None,
        language: str | None,
        topics: list[str],
        readme_excerpt: str | None,
        available_lists: list[StarListCandidate],
        correlation_id: str | None,
    ) -> dict[str, float]:
        """Score each live list by the summed similarity of neighbours in it."""
        query = _build_query(
            description=description,
            language=language,
            topics=topics,
            readme_excerpt=readme_excerpt,
        )
        if not query:
            return {}

        try:
            results = await self._neighbour_search.search(
                query,
                user_id=user_id,
                # One extra slot: the subject itself is usually its own closest
                # match once it has been embedded.
                limit=self._neighbour_limit + 1,
                min_similarity=self._min_similarity,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            # An unavailable vector store must not fail the add; the LLM fallback
            # still has a chance, and "no list" is an acceptable outcome.
            logger.warning(
                "star_list_neighbour_search_failed",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )
            return {}

        live = {candidate.name for candidate in available_lists}
        scores: dict[str, float] = {}
        for item in getattr(results, "items", []) or []:
            if int(getattr(item, "repository_id", 0)) == repository_id:
                continue
            similarity = 1.0 - float(getattr(item, "distance", 1.0))
            if similarity <= 0.0:
                continue
            for name in getattr(item, "list_names", None) or []:
                # A neighbour may still carry a list the user has since renamed
                # or deleted; only live names can be written back.
                if name in live:
                    scores[name] = scores.get(name, 0.0) + similarity
        return scores

    def _decide(self, scores: dict[str, float]) -> tuple[str, float] | None:
        """Accept the top list only when it is both strong and clearly ahead."""
        if not scores:
            return None
        ranked = sorted(scores.items(), key=lambda entry: entry[1], reverse=True)
        top_name, top_score = ranked[0]
        if top_score < self._min_score:
            return None
        if len(ranked) > 1:
            runner_up = ranked[1][1]
            if runner_up > 0 and top_score < runner_up * self._dominance_ratio:
                return None
        return top_name, top_score

    async def _classify(
        self,
        *,
        full_name: str,
        description: str | None,
        language: str | None,
        topics: list[str],
        readme_excerpt: str | None,
        available_lists: list[StarListCandidate],
        correlation_id: str | None,
    ) -> StarListChoice | None:
        if self._classifier is None or not available_lists:
            return None
        choice = await self._classifier.classify(
            candidates=list(available_lists),
            full_name=full_name,
            description=description,
            language=language,
            topics=topics,
            readme_excerpt=readme_excerpt,
            correlation_id=correlation_id,
        )
        # An empty list_name is the model correctly saying "none of these fit".
        if choice is None or not choice.list_name.strip():
            return None
        return choice


def _build_query(
    *,
    description: str | None,
    language: str | None,
    topics: list[str],
    readme_excerpt: str | None,
) -> str:
    """Assemble the text whose embedding is compared against the user's repos."""
    parts = [
        (description or "").strip(),
        " ".join(topics),
        (language or "").strip(),
        (readme_excerpt or "").strip()[:_QUERY_README_BUDGET],
    ]
    return " ".join(part for part in parts if part).strip()
