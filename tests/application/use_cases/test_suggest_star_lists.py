"""Tests for SuggestStarListsUseCase — weighted kNN vote with an LLM fallback."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.use_cases.suggest_star_lists import SuggestStarListsUseCase
from app.core.star_list_suggestion_schema import StarListCandidate, StarListChoice

_LISTS = [
    StarListCandidate(name="Android"),
    StarListCandidate(name="Kubernetes"),
    StarListCandidate(name="Rust"),
]


@dataclass(frozen=True)
class _Neighbour:
    repository_id: int
    list_names: list[str]
    distance: float


def _search(neighbours: list[_Neighbour]) -> MagicMock:
    port = MagicMock()
    port.search = AsyncMock(return_value=MagicMock(items=neighbours))
    return port


def _classifier(choice: StarListChoice | None) -> MagicMock:
    port = MagicMock()
    port.classify = AsyncMock(return_value=choice)
    return port


async def _suggest(use_case: SuggestStarListsUseCase, *, repository_id: int = 99):
    return await use_case.suggest(
        user_id=42,
        repository_id=repository_id,
        full_name="square/metro",
        description="A Kubernetes operator",
        language="Rust",
        topics=["kubernetes", "operator"],
        readme_excerpt="Deploys workloads.",
        available_lists=_LISTS,
    )


@pytest.mark.asyncio
async def test_one_strong_neighbour_beats_three_weak_ones():
    """The vote is weighted by similarity; a raw count would invert this."""
    use_case = SuggestStarListsUseCase(
        neighbour_search=_search(
            [
                # similarity 0.90
                _Neighbour(1, ["Kubernetes"], 0.10),
                # similarity 0.20 each -> 0.60 total, below the 0.90 winner
                _Neighbour(2, ["Rust"], 0.80),
                _Neighbour(3, ["Rust"], 0.80),
                _Neighbour(4, ["Rust"], 0.80),
            ]
        ),
        min_score=0.5,
    )

    result = await _suggest(use_case)

    assert result.list_names == ["Kubernetes"]
    assert result.source == "knn"


@pytest.mark.asyncio
async def test_a_weak_vote_falls_back_to_the_llm():
    use_case = SuggestStarListsUseCase(
        neighbour_search=_search([_Neighbour(1, ["Rust"], 0.7)]),
        classifier=_classifier(StarListChoice(list_name="Kubernetes", confidence=0.8)),
        min_score=1.0,
    )

    result = await _suggest(use_case)

    assert result.list_names == ["Kubernetes"]
    assert result.source == "llm"


@pytest.mark.asyncio
async def test_a_near_tie_is_inconclusive_even_when_strong():
    """Two lists neck and neck means the neighbours do not actually agree."""
    classifier = _classifier(None)
    use_case = SuggestStarListsUseCase(
        neighbour_search=_search(
            [
                _Neighbour(1, ["Kubernetes"], 0.1),  # 0.9
                _Neighbour(2, ["Rust"], 0.15),  # 0.85
            ]
        ),
        min_score=0.5,
        classifier=classifier,
    )

    result = await _suggest(use_case)

    assert result.source == "none"
    classifier.classify.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_dominant_winner_is_accepted_over_a_runner_up():
    use_case = SuggestStarListsUseCase(
        neighbour_search=_search(
            [
                _Neighbour(1, ["Kubernetes"], 0.1),  # 0.90
                _Neighbour(2, ["Kubernetes"], 0.2),  # 0.80 -> 1.70 total
                _Neighbour(3, ["Rust"], 0.9),  # 0.10
            ]
        ),
        min_score=1.0,
    )

    result = await _suggest(use_case)

    assert result.list_names == ["Kubernetes"]
    assert result.scores["Kubernetes"] == pytest.approx(1.7)


@pytest.mark.asyncio
async def test_the_repository_does_not_vote_for_itself():
    """Once embedded, a repository is its own nearest neighbour."""
    use_case = SuggestStarListsUseCase(
        neighbour_search=_search([_Neighbour(99, ["Android"], 0.0)]),
        min_score=0.5,
    )

    result = await _suggest(use_case, repository_id=99)

    assert result.source == "none"
    assert result.scores == {}


@pytest.mark.asyncio
async def test_a_stale_list_name_is_ignored():
    """A neighbour can still carry a list the user has since renamed or deleted."""
    use_case = SuggestStarListsUseCase(
        neighbour_search=_search([_Neighbour(1, ["Deleted List"], 0.05)]),
        min_score=0.5,
    )

    result = await _suggest(use_case)

    assert result.source == "none"
    assert result.scores == {}


@pytest.mark.asyncio
async def test_no_neighbours_hands_over_to_the_llm():
    use_case = SuggestStarListsUseCase(
        neighbour_search=_search([]),
        classifier=_classifier(StarListChoice(list_name="Android", confidence=0.6)),
    )

    result = await _suggest(use_case)

    assert result.list_names == ["Android"]
    assert result.source == "llm"


@pytest.mark.asyncio
async def test_an_empty_llm_pick_means_no_list():
    """ "None of these fit" is a valid, useful answer."""
    use_case = SuggestStarListsUseCase(
        neighbour_search=_search([]),
        classifier=_classifier(StarListChoice(list_name="", confidence=0.9, reason="no fit")),
    )

    result = await _suggest(use_case)

    assert result.list_names == []
    assert result.source == "none"


@pytest.mark.asyncio
async def test_without_a_classifier_an_inconclusive_vote_leaves_it_unfiled():
    use_case = SuggestStarListsUseCase(neighbour_search=_search([]), classifier=None)

    result = await _suggest(use_case)

    assert result.list_names == []
    assert result.source == "none"


@pytest.mark.asyncio
async def test_an_unavailable_vector_store_does_not_fail_the_add():
    search = MagicMock()
    search.search = AsyncMock(side_effect=RuntimeError("qdrant down"))
    use_case = SuggestStarListsUseCase(
        neighbour_search=search,
        classifier=_classifier(StarListChoice(list_name="Android", confidence=0.7)),
    )

    result = await _suggest(use_case)

    # The kNN half degraded silently; the LLM still answered.
    assert result.list_names == ["Android"]
    assert result.source == "llm"


@pytest.mark.asyncio
async def test_an_empty_query_skips_the_search_entirely():
    """A repository with no description, topics, language, or README has no signal."""
    search = _search([_Neighbour(1, ["Android"], 0.0)])
    use_case = SuggestStarListsUseCase(neighbour_search=search)

    result = await use_case.suggest(
        user_id=42,
        repository_id=7,
        full_name="ghost/empty",
        description=None,
        language=None,
        topics=[],
        readme_excerpt=None,
        available_lists=_LISTS,
    )

    assert result.source == "none"
    search.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_search_reserves_a_slot_for_the_subject_itself():
    search = _search([])
    use_case = SuggestStarListsUseCase(neighbour_search=search, neighbour_limit=15)

    await _suggest(use_case)

    assert search.search.await_args.kwargs["limit"] == 16
