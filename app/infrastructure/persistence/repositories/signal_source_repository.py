"""SQLAlchemy repository for Phase 3 signal-source entities."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from app.db.models import FeedItem, Source, Subscription, Topic, UserSignal, model_to_dict
from app.db.types import _utcnow

if TYPE_CHECKING:
    from app.db.session import Database


class SignalSourceRepositoryAdapter:
    """Persistence adapter for sources, subscriptions, topics, and signals."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_upsert_source(
        self,
        *,
        kind: str,
        external_id: str | None = None,
        url: str | None = None,
        title: str | None = None,
        description: str | None = None,
        site_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._database.transaction() as session:
            now = _utcnow()
            existing = await session.scalar(
                select(Source).where(Source.kind == kind, Source.external_id == external_id)
            )
            if existing is not None:
                existing.url = url
                existing.title = title
                existing.description = description
                existing.site_url = site_url
                existing.metadata_json = _merge_preserving_controls(
                    existing.metadata_json, metadata
                )
                existing.updated_at = now
                return model_to_dict(existing) or {}

            stmt = insert(Source).values(
                kind=kind,
                external_id=external_id,
                url=url,
                title=title,
                description=description,
                site_url=site_url,
                metadata_json=metadata,
                updated_at=now,
            )
            update_values = {
                "url": stmt.excluded.url,
                "title": stmt.excluded.title,
                "description": stmt.excluded.description,
                "site_url": stmt.excluded.site_url,
                "updated_at": now,
            }
            source = await session.scalar(
                stmt.on_conflict_do_update(
                    index_elements=[Source.kind, Source.external_id],
                    set_=update_values,
                ).returning(Source)
            )
            return model_to_dict(source) or {}

    async def async_subscribe(
        self,
        *,
        user_id: int,
        source_id: int,
        topic_constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._database.transaction() as session:
            now = _utcnow()
            stmt = insert(Subscription).values(
                user_id=user_id,
                source_id=source_id,
                topic_constraints_json=topic_constraints,
                is_active=True,
                updated_at=now,
            )
            subscription = await session.scalar(
                stmt.on_conflict_do_update(
                    index_elements=[Subscription.user_id, Subscription.source_id],
                    set_={
                        "topic_constraints_json": stmt.excluded.topic_constraints_json,
                        "is_active": True,
                        "updated_at": now,
                    },
                ).returning(Subscription)
            )
            return self._subscription_dict(subscription)

    async def async_subscribe_many(
        self,
        *,
        source_id: int,
        user_ids: list[int],
        topic_constraints: dict[str, Any] | None = None,
    ) -> None:
        if not user_ids:
            return

        values: list[dict[str, Any]] = []
        seen_user_ids: set[int] = set()
        for user_id in user_ids:
            normalized_user_id = int(user_id)
            if normalized_user_id in seen_user_ids:
                continue
            seen_user_ids.add(normalized_user_id)
            values.append(
                {
                    "user_id": normalized_user_id,
                    "source_id": source_id,
                    "topic_constraints_json": topic_constraints,
                    "is_active": True,
                    "updated_at": _utcnow(),
                }
            )

        if not values:
            return

        async with self._database.transaction() as session:
            now = _utcnow()
            stmt = insert(Subscription).values(values)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[Subscription.user_id, Subscription.source_id],
                    set_={
                        "topic_constraints_json": stmt.excluded.topic_constraints_json,
                        "is_active": True,
                        "updated_at": now,
                    },
                )
            )

    async def async_get_source(self, source_id: int) -> dict[str, Any] | None:
        async with self._database.session() as session:
            source = await session.get(Source, source_id)
            return model_to_dict(source)

    async def async_set_source_active(self, source_id: int, *, is_active: bool) -> bool:
        async with self._database.transaction() as session:
            updated_id = await session.scalar(
                update(Source)
                .where(Source.id == source_id)
                .values(is_active=is_active, updated_at=_utcnow())
                .returning(Source.id)
            )
            return updated_id is not None

    async def async_set_user_source_active(
        self,
        *,
        user_id: int,
        source_id: int,
        is_active: bool,
    ) -> bool:
        async with self._database.transaction() as session:
            subscription_id = await session.scalar(
                select(Subscription.id).where(
                    Subscription.user_id == user_id,
                    Subscription.source_id == source_id,
                )
            )
            if subscription_id is None:
                return False
            updated_id = await session.scalar(
                update(Source)
                .where(Source.id == source_id)
                .values(is_active=is_active, updated_at=_utcnow())
                .returning(Source.id)
            )
            return updated_id is not None

    async def async_update_user_source_controls(
        self,
        *,
        user_id: int,
        source_id: int,
        is_active: bool | None = None,
        fetch_interval_seconds: int | None = None,
        max_items_per_run: int | None = None,
        retry_policy: dict[str, Any] | None = None,
    ) -> bool:
        """Update source/subscription controls when the user can see the source."""
        async with self._database.transaction() as session:
            row = (
                await session.execute(
                    select(Source, Subscription).where(
                        Source.id == source_id,
                        Subscription.source_id == Source.id,
                        Subscription.user_id == user_id,
                    )
                )
            ).first()
            if row is None:
                return False

            source, subscription = row
            metadata = dict(source.metadata_json or {})
            controls = dict(metadata.get("controls") or {})

            if is_active is not None:
                source.is_active = is_active
                subscription.is_active = is_active
                if is_active:
                    subscription.next_fetch_at = None
            if fetch_interval_seconds is not None:
                value = max(300, min(int(fetch_interval_seconds), 604800))
                subscription.cadence_seconds = value
                controls["fetch_interval_seconds"] = value
            if max_items_per_run is not None:
                controls["max_items_per_run"] = max(1, min(int(max_items_per_run), 500))
            if retry_policy is not None:
                controls["retry_policy"] = retry_policy

            metadata["controls"] = controls
            source.metadata_json = metadata
            now = _utcnow()
            source.updated_at = now
            subscription.updated_at = now
            return True

    async def async_retry_user_source(self, *, user_id: int, source_id: int) -> bool:
        """Reactivate a visible source and clear subscription backoff."""
        async with self._database.transaction() as session:
            row = (
                await session.execute(
                    select(Source, Subscription).where(
                        Source.id == source_id,
                        Subscription.source_id == Source.id,
                        Subscription.user_id == user_id,
                    )
                )
            ).first()
            if row is None:
                return False
            source, subscription = row
            now = _utcnow()
            source.is_active = True
            source.updated_at = now
            subscription.is_active = True
            subscription.next_fetch_at = None
            subscription.updated_at = now
            return True

    async def async_get_source_run_state(self, source_id: int) -> dict[str, Any] | None:
        """Return persisted scheduler controls for a source."""
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(Source, Subscription)
                    .outerjoin(Subscription, Subscription.source_id == Source.id)
                    .where(Source.id == source_id)
                    .order_by(Subscription.next_fetch_at.asc().nulls_first())
                    .limit(1)
                )
            ).first()
            if row is None:
                return None
            source, subscription = row
            metadata = dict(source.metadata_json or {})
            controls = dict(metadata.get("controls") or {})
            return {
                "source_id": source.id,
                "is_active": source.is_active,
                "active_subscription": bool(subscription is not None and subscription.is_active),
                "backoff_until": subscription.next_fetch_at if subscription is not None else None,
                "fetch_interval_seconds": (
                    subscription.cadence_seconds if subscription is not None else None
                )
                or controls.get("fetch_interval_seconds"),
                "max_items_per_run": controls.get("max_items_per_run"),
                "retry_policy": controls.get("retry_policy"),
            }

    async def async_record_source_fetch_success(self, source_id: int) -> None:
        now = _utcnow()
        async with self._database.transaction() as session:
            await session.execute(
                update(Source)
                .where(Source.id == source_id)
                .values(
                    fetch_error_count=0,
                    last_error=None,
                    last_fetched_at=now,
                    last_successful_at=now,
                    updated_at=now,
                )
            )
            await session.execute(
                update(Subscription)
                .where(Subscription.source_id == source_id)
                .values(next_fetch_at=None, updated_at=now)
            )

    async def async_record_source_fetch_error(
        self,
        *,
        source_id: int,
        error: str,
        max_errors: int,
        base_backoff_seconds: int,
    ) -> bool:
        async with self._database.transaction() as session:
            now = _utcnow()
            error_count = int(
                await session.scalar(select(Source.fetch_error_count).where(Source.id == source_id))
                or 0
            )
            error_count += 1
            disabled = error_count >= max_errors
            backoff_seconds = min(base_backoff_seconds * (2 ** max(0, error_count - 1)), 86400)
            next_fetch_at = now + timedelta(seconds=backoff_seconds)
            await session.execute(
                update(Source)
                .where(Source.id == source_id)
                .values(
                    fetch_error_count=error_count,
                    last_error=error[:500],
                    last_fetched_at=now,
                    is_active=not disabled,
                    updated_at=now,
                )
            )
            await session.execute(
                update(Subscription)
                .where(Subscription.source_id == source_id)
                .values(next_fetch_at=next_fetch_at, updated_at=now)
            )
            return disabled

    async def async_set_subscription_active(
        self,
        *,
        user_id: int,
        subscription_id: int,
        is_active: bool,
    ) -> bool:
        async with self._database.transaction() as session:
            updated_id = await session.scalar(
                update(Subscription)
                .where(Subscription.id == subscription_id, Subscription.user_id == user_id)
                .values(is_active=is_active, updated_at=_utcnow())
                .returning(Subscription.id)
            )
            return updated_id is not None

    async def async_upsert_feed_item(
        self,
        *,
        source_id: int,
        external_id: str,
        canonical_url: str | None = None,
        title: str | None = None,
        content_text: str | None = None,
        author: str | None = None,
        published_at: Any | None = None,
        engagement: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        engagement = engagement or {}
        async with self._database.transaction() as session:
            now = _utcnow()
            stmt = insert(FeedItem).values(
                source_id=source_id,
                external_id=external_id,
                canonical_url=canonical_url,
                title=title,
                content_text=content_text,
                author=author,
                published_at=published_at,
                views=engagement.get("views"),
                forwards=engagement.get("forwards"),
                comments=engagement.get("comments"),
                engagement_score=engagement.get("score"),
                metadata_json=metadata,
                updated_at=now,
            )
            item = await session.scalar(
                stmt.on_conflict_do_update(
                    index_elements=[FeedItem.source_id, FeedItem.external_id],
                    set_={
                        "canonical_url": stmt.excluded.canonical_url,
                        "title": stmt.excluded.title,
                        "content_text": stmt.excluded.content_text,
                        "author": stmt.excluded.author,
                        "published_at": stmt.excluded.published_at,
                        "views": stmt.excluded.views,
                        "forwards": stmt.excluded.forwards,
                        "comments": stmt.excluded.comments,
                        "engagement_score": stmt.excluded.engagement_score,
                        "metadata_json": stmt.excluded.metadata_json,
                        "updated_at": now,
                    },
                ).returning(FeedItem)
            )
            return self._feed_item_dict(item)

    async def async_upsert_feed_items(
        self,
        *,
        source_id: int,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not items:
            return []

        values: list[dict[str, Any]] = []
        seen_external_ids: set[str] = set()
        for item in items:
            external_id = str(item["external_id"])
            if external_id in seen_external_ids:
                continue
            seen_external_ids.add(external_id)
            engagement = item.get("engagement") or {}
            values.append(
                {
                    "source_id": source_id,
                    "external_id": external_id,
                    "canonical_url": item.get("canonical_url"),
                    "title": item.get("title"),
                    "content_text": item.get("content_text"),
                    "author": item.get("author"),
                    "published_at": item.get("published_at"),
                    "views": engagement.get("views"),
                    "forwards": engagement.get("forwards"),
                    "comments": engagement.get("comments"),
                    "engagement_score": engagement.get("score"),
                    "metadata_json": item.get("metadata"),
                    "updated_at": _utcnow(),
                }
            )

        if not values:
            return []

        async with self._database.transaction() as session:
            now = _utcnow()
            stmt = insert(FeedItem).values(values)
            rows = list(
                (
                    await session.execute(
                        stmt.on_conflict_do_update(
                            index_elements=[FeedItem.source_id, FeedItem.external_id],
                            set_={
                                "canonical_url": stmt.excluded.canonical_url,
                                "title": stmt.excluded.title,
                                "content_text": stmt.excluded.content_text,
                                "author": stmt.excluded.author,
                                "published_at": stmt.excluded.published_at,
                                "views": stmt.excluded.views,
                                "forwards": stmt.excluded.forwards,
                                "comments": stmt.excluded.comments,
                                "engagement_score": stmt.excluded.engagement_score,
                                "metadata_json": stmt.excluded.metadata_json,
                                "updated_at": now,
                            },
                        ).returning(FeedItem)
                    )
                ).scalars()
            )

        rows_by_external_id = {row.external_id: self._feed_item_dict(row) for row in rows}
        return [
            rows_by_external_id[record["external_id"]]
            for record in values
            if record["external_id"] in rows_by_external_id
        ]

    async def async_upsert_topic(
        self,
        *,
        user_id: int,
        name: str,
        description: str | None = None,
        weight: float = 1.0,
        embedding_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._database.transaction() as session:
            now = _utcnow()
            stmt = insert(Topic).values(
                user_id=user_id,
                name=name,
                description=description,
                weight=weight,
                embedding_ref=embedding_ref,
                metadata_json=metadata,
                is_active=True,
                updated_at=now,
            )
            topic = await session.scalar(
                stmt.on_conflict_do_update(
                    index_elements=[Topic.user_id, Topic.name],
                    set_={
                        "description": stmt.excluded.description,
                        "weight": stmt.excluded.weight,
                        "embedding_ref": stmt.excluded.embedding_ref,
                        "metadata_json": stmt.excluded.metadata_json,
                        "is_active": True,
                        "updated_at": now,
                    },
                ).returning(Topic)
            )
            return self._topic_dict(topic)

    async def async_record_user_signal(
        self,
        *,
        user_id: int,
        feed_item_id: int,
        topic_id: int | None = None,
        status: str = "candidate",
        heuristic_score: float | None = None,
        llm_score: float | None = None,
        final_score: float | None = None,
        evidence: dict[str, Any] | None = None,
        filter_stage: str = "heuristic",
        llm_judge: dict[str, Any] | None = None,
        llm_cost_usd: float | None = None,
    ) -> dict[str, Any]:
        async with self._database.transaction() as session:
            now = _utcnow()
            stmt = insert(UserSignal).values(
                user_id=user_id,
                feed_item_id=feed_item_id,
                topic_id=topic_id,
                status=status,
                heuristic_score=heuristic_score,
                llm_score=llm_score,
                final_score=final_score,
                evidence_json=evidence,
                filter_stage=filter_stage,
                llm_judge_json=llm_judge,
                llm_cost_usd=llm_cost_usd,
                updated_at=now,
            )
            signal = await session.scalar(
                stmt.on_conflict_do_update(
                    index_elements=[UserSignal.user_id, UserSignal.feed_item_id],
                    set_={
                        "topic_id": stmt.excluded.topic_id,
                        "status": stmt.excluded.status,
                        "heuristic_score": stmt.excluded.heuristic_score,
                        "llm_score": stmt.excluded.llm_score,
                        "final_score": stmt.excluded.final_score,
                        "evidence_json": stmt.excluded.evidence_json,
                        "filter_stage": stmt.excluded.filter_stage,
                        "llm_judge_json": stmt.excluded.llm_judge_json,
                        "llm_cost_usd": stmt.excluded.llm_cost_usd,
                        "updated_at": now,
                    },
                ).returning(UserSignal)
            )
            return self._signal_dict(signal)

    async def async_list_user_subscriptions(self, user_id: int) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = await session.execute(
                select(Subscription, Source)
                .join(Source, Subscription.source_id == Source.id)
                .where(Subscription.user_id == user_id)
                .order_by(Subscription.created_at.desc())
            )
            result: list[dict[str, Any]] = []
            for subscription, source in rows:
                data = self._subscription_dict(subscription)
                data["source_kind"] = source.kind
                data["source_title"] = source.title
                data["source_url"] = source.url
                data["source_external_id"] = source.external_id
                result.append(data)
            return result

    async def async_list_source_health(self, *, user_id: int) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = await session.execute(
                select(Subscription, Source)
                .join(Source, Subscription.source_id == Source.id)
                .where(Subscription.user_id == user_id)
                .order_by(Source.is_active.asc(), Source.fetch_error_count.desc(), Source.title)
            )
            return [
                {
                    "id": source.id,
                    "kind": source.kind,
                    "external_id": source.external_id,
                    "url": source.url,
                    "title": source.title,
                    "is_active": source.is_active,
                    "fetch_error_count": source.fetch_error_count,
                    "last_error": source.last_error,
                    "last_fetched_at": source.last_fetched_at,
                    "last_successful_at": source.last_successful_at,
                    "last_failure_at": source.updated_at
                    if source.last_error or int(source.fetch_error_count or 0) > 0
                    else None,
                    "subscription_id": subscription.id,
                    "subscription_active": subscription.is_active,
                    "cadence_seconds": subscription.cadence_seconds,
                    "next_fetch_at": subscription.next_fetch_at,
                    "backoff_until": subscription.next_fetch_at,
                    "fetch_interval_seconds": subscription.cadence_seconds,
                    "max_items_per_run": (source.metadata_json or {})
                    .get("controls", {})
                    .get("max_items_per_run"),
                    "retry_policy": (source.metadata_json or {})
                    .get("controls", {})
                    .get("retry_policy"),
                }
                for subscription, source in rows
            ]

    async def async_list_user_signals(
        self,
        user_id: int,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            stmt = (
                select(UserSignal, FeedItem, Source, Topic)
                .join(FeedItem, UserSignal.feed_item_id == FeedItem.id)
                .join(Source, FeedItem.source_id == Source.id)
                .outerjoin(Topic, UserSignal.topic_id == Topic.id)
                .where(UserSignal.user_id == user_id)
                .order_by(UserSignal.final_score.desc().nulls_last(), UserSignal.created_at.desc())
                .limit(limit)
            )
            if status is not None:
                stmt = stmt.where(UserSignal.status == status)
            rows = await session.execute(stmt)
            result: list[dict[str, Any]] = []
            for signal, item, source, topic in rows:
                data = self._signal_dict(signal)
                data["feed_item_title"] = item.title
                data["feed_item_url"] = item.canonical_url
                data["source_kind"] = source.kind
                data["source_title"] = source.title
                data["topic_name"] = topic.name if topic is not None else None
                result.append(data)
            return result

    async def async_get_user_signal(self, *, user_id: int, signal_id: int) -> dict[str, Any] | None:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(UserSignal, FeedItem, Source, Topic)
                    .join(FeedItem, UserSignal.feed_item_id == FeedItem.id)
                    .join(Source, FeedItem.source_id == Source.id)
                    .outerjoin(Topic, UserSignal.topic_id == Topic.id)
                    .where(UserSignal.id == signal_id, UserSignal.user_id == user_id)
                )
            ).first()
            if row is None:
                return None
            signal, item, source, topic = row
            data = self._signal_dict(signal)
            data["feed_item_id"] = item.id
            data["feed_item_title"] = item.title
            data["feed_item_url"] = item.canonical_url
            data["feed_item_content_text"] = item.content_text
            data["source_kind"] = source.kind
            data["source_title"] = source.title
            data["topic_name"] = topic.name if topic is not None else None
            return data

    async def async_update_user_signal_status(
        self,
        *,
        user_id: int,
        signal_id: int,
        status: str,
    ) -> bool:
        async with self._database.transaction() as session:
            now = _utcnow()
            updated_id = await session.scalar(
                update(UserSignal)
                .where(UserSignal.id == signal_id, UserSignal.user_id == user_id)
                .values(status=status, decided_at=now, updated_at=now)
                .returning(UserSignal.id)
            )
            return updated_id is not None

    async def async_hide_signal_source(self, *, user_id: int, signal_id: int) -> bool:
        async with self._database.transaction() as session:
            row = (
                await session.execute(
                    select(UserSignal, FeedItem)
                    .join(FeedItem, UserSignal.feed_item_id == FeedItem.id)
                    .where(UserSignal.id == signal_id, UserSignal.user_id == user_id)
                )
            ).first()
            if row is None:
                return False
            signal, item = row
            now = _utcnow()
            await session.execute(
                update(Source)
                .where(Source.id == item.source_id)
                .values(is_active=False, updated_at=now)
            )
            signal.status = "hidden_source"
            signal.decided_at = now
            signal.updated_at = now
            return True

    async def async_boost_signal_topic(
        self,
        *,
        user_id: int,
        signal_id: int,
        increment: float = 0.25,
    ) -> bool:
        async with self._database.transaction() as session:
            row = (
                await session.execute(
                    select(UserSignal, Topic)
                    .join(Topic, UserSignal.topic_id == Topic.id)
                    .where(
                        UserSignal.id == signal_id,
                        UserSignal.user_id == user_id,
                        Topic.user_id == user_id,
                    )
                )
            ).first()
            if row is None:
                return False
            signal, topic = row
            now = _utcnow()
            topic.weight = min(5.0, float(topic.weight or 0.0) + max(0.0, float(increment)))
            topic.updated_at = now
            signal.status = "boosted_topic"
            signal.decided_at = now
            signal.updated_at = now
            return True

    async def async_list_unscored_candidates(self, *, limit: int = 100) -> list[dict[str, Any]]:
        signal_alias = aliased(UserSignal)
        async with self._database.session() as session:
            rows = await session.execute(
                select(FeedItem, Source, Subscription)
                .join(Source, FeedItem.source_id == Source.id)
                .join(Subscription, Subscription.source_id == FeedItem.source_id)
                .outerjoin(
                    signal_alias,
                    (signal_alias.feed_item_id == FeedItem.id)
                    & (signal_alias.user_id == Subscription.user_id),
                )
                .where(
                    Source.is_active.is_(True),
                    Subscription.is_active.is_(True),
                    signal_alias.id.is_(None),
                )
                .order_by(FeedItem.published_at.desc().nulls_last(), FeedItem.created_at.desc())
                .limit(limit)
            )
            return [
                {
                    "user_id": subscription.user_id,
                    "source_id": item.source_id,
                    "source_kind": source.kind,
                    "feed_item_id": item.id,
                    "title": item.title,
                    "canonical_url": item.canonical_url,
                    "content_text": item.content_text,
                    "published_at": item.published_at,
                    "views": item.views,
                    "forwards": item.forwards,
                    "comments": item.comments,
                }
                for item, source, subscription in rows
            ]

    @staticmethod
    def _subscription_dict(subscription: Subscription | None) -> dict[str, Any]:
        data = model_to_dict(subscription) or {}
        if data:
            data["user"] = data.get("user_id")
            data["source"] = data.get("source_id")
        return data

    @staticmethod
    def _feed_item_dict(item: FeedItem | None) -> dict[str, Any]:
        data = model_to_dict(item) or {}
        if data:
            data["source"] = data.get("source_id")
        return data

    @staticmethod
    def _topic_dict(topic: Topic | None) -> dict[str, Any]:
        data = model_to_dict(topic) or {}
        if data:
            data["user"] = data.get("user_id")
        return data

    @staticmethod
    def _signal_dict(signal: UserSignal | None) -> dict[str, Any]:
        data = model_to_dict(signal) or {}
        if data:
            data["user"] = data.get("user_id")
            data["feed_item"] = data.get("feed_item_id")
            data["topic"] = data.get("topic_id")
        return data


def _merge_preserving_controls(
    current_metadata: Any,
    incoming_metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    incoming = dict(incoming_metadata or {})
    if isinstance(current_metadata, dict) and "controls" in current_metadata:
        incoming.setdefault("controls", current_metadata["controls"])
    return incoming or None
