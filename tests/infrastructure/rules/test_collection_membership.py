from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import cast

import pytest

from app.db.session import Database
from app.infrastructure.rules.collection_membership import CollectionMembershipAdapter


class _Session:
    def __init__(self, values: list[int | None]) -> None:
        self.values = values

    async def scalar(self, stmt: object) -> int | None:
        return self.values.pop(0)


class _Database:
    def __init__(self, values: list[int | None]) -> None:
        self.session = _Session(values)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_Session]:
        yield self.session


@pytest.mark.asyncio
async def test_collection_membership_add_results() -> None:
    assert (
        await CollectionMembershipAdapter(cast("Database", _Database([None]))).async_add_summary(
            user_id=1,
            collection_id=2,
            summary_id=3,
        )
        == "collection 2 not found or not owned by user"
    )

    assert (
        await CollectionMembershipAdapter(cast("Database", _Database([2, None]))).async_add_summary(
            user_id=1,
            collection_id=2,
            summary_id=3,
        )
        == "already in collection 2"
    )

    assert (
        await CollectionMembershipAdapter(cast("Database", _Database([2, 9]))).async_add_summary(
            user_id=1,
            collection_id=2,
            summary_id=3,
        )
        == "added to collection 2"
    )


@pytest.mark.asyncio
async def test_collection_membership_remove_results() -> None:
    assert (
        await CollectionMembershipAdapter(cast("Database", _Database([None]))).async_remove_summary(
            user_id=1,
            collection_id=2,
            summary_id=3,
        )
        == "collection 2 not found or not owned by user"
    )

    assert (
        await CollectionMembershipAdapter(cast("Database", _Database([2, None]))).async_remove_summary(
            user_id=1,
            collection_id=2,
            summary_id=3,
        )
        == "not in collection"
    )

    assert (
        await CollectionMembershipAdapter(cast("Database", _Database([2, 9]))).async_remove_summary(
            user_id=1,
            collection_id=2,
            summary_id=3,
        )
        == "removed from collection 2"
    )
