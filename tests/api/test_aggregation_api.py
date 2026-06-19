from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.routers.auth.tokens import create_access_token
from app.application.dto.aggregation import (
    MultiSourceAggregationOutput,
    MultiSourceExtractionOutput,
    SourceCoverageEntry,
    SourceExtractionItemResult,
)
from app.application.services.aggregation_rollout import (
    AggregationRolloutDecision,
    AggregationRolloutStage,
)
from app.application.services.multi_source_aggregation_service import (
    MultiSourceAggregationRunResult,
)
from app.config import Config, load_config
from app.di.repositories import build_aggregation_session_repository
from app.domain.models.source import AggregationSessionStatus, SourceItem, SourceKind


def _auth_headers(user_id: int, client_id: str = "test") -> dict[str, str]:
    token = create_access_token(user_id, client_id=client_id)
    return {"Authorization": f"Bearer {token}"}


def _allow_public_urls():
    return patch("app.api.routers.aggregation.is_url_safe", return_value=(True, None))


def _set_runtime(client, db) -> SimpleNamespace | None:
    runtime = getattr(client.app.state, "runtime", None)
    client.app.state.runtime = SimpleNamespace(
        cfg=load_config(allow_stub_telegram=True),
        db=db,
        background_processor=SimpleNamespace(
            url_processor=SimpleNamespace(content_extractor=MagicMock())
        ),
        core=SimpleNamespace(llm_client=MagicMock()),
    )
    return runtime


def test_create_aggregation_bundle_endpoint_returns_session_and_items(client, db, user_factory):

    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424242
    user_factory(username="aggregation_api_user", telegram_user_id=user_id)

    fake_result = MultiSourceAggregationRunResult(
        extraction=MultiSourceExtractionOutput(
            session_id=77,
            correlation_id="cid-agg-create",
            status="completed",
            successful_count=2,
            failed_count=0,
            duplicate_count=0,
            items=[
                SourceExtractionItemResult(
                    position=0,
                    item_id=1001,
                    source_item_id="src_a",
                    source_kind=SourceKind.WEB_ARTICLE,
                    status="extracted",
                    request_id=501,
                ),
                SourceExtractionItemResult(
                    position=1,
                    item_id=1002,
                    source_item_id="src_b",
                    source_kind=SourceKind.X_POST,
                    status="extracted",
                    request_id=502,
                ),
            ],
        ),
        aggregation=MultiSourceAggregationOutput(
            session_id=77,
            correlation_id="cid-agg-create",
            status="completed",
            source_type="mixed",
            total_items=2,
            extracted_items=2,
            used_source_count=2,
            overview="Two-source synthesis",
            source_coverage=[
                SourceCoverageEntry(
                    position=0,
                    item_id=1001,
                    source_item_id="src_a",
                    source_kind=SourceKind.WEB_ARTICLE,
                    status="extracted",
                    used_in_summary=True,
                ),
                SourceCoverageEntry(
                    position=1,
                    item_id=1002,
                    source_item_id="src_b",
                    source_kind=SourceKind.X_POST,
                    status="extracted",
                    used_in_summary=True,
                ),
            ],
        ),
    )

    runtime = _set_runtime(client, db)
    try:
        with (
            patch(
                "app.application.services.multi_source_aggregation_service.MultiSourceAggregationService.aggregate",
                new=AsyncMock(return_value=fake_result),
            ),
            _allow_public_urls(),
        ):
            response = client.post(
                "/v1/aggregations",
                headers=_auth_headers(user_id),
                json={
                    "items": [
                        {"url": "https://example.com/article"},
                        {"url": "https://x.com/example/status/1"},
                    ],
                    "lang_preference": "en",
                },
            )
    finally:
        client.app.state.runtime = runtime

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["session"]["sessionId"] == 77
    assert payload["data"]["session"]["sourceType"] == "mixed"
    assert payload["data"]["session"]["progress"]["completionPercent"] == 100
    assert payload["data"]["aggregation"]["overview"] == "Two-source synthesis"
    assert [item["sourceKind"] for item in payload["data"]["items"]] == [
        "web_article",
        "x_post",
    ]


def test_create_aggregation_bundle_endpoint_audits_and_passes_client_id_metadata(
    client, db, user_factory
):
    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424242
    user_factory(username="aggregation_api_audit_user", telegram_user_id=user_id)

    fake_result = MultiSourceAggregationRunResult(
        extraction=MultiSourceExtractionOutput(
            session_id=701,
            correlation_id="cid-agg-audit",
            status="completed",
            successful_count=1,
            failed_count=0,
            duplicate_count=0,
            items=[
                SourceExtractionItemResult(
                    position=0,
                    item_id=9001,
                    source_item_id="src_audit",
                    source_kind=SourceKind.WEB_ARTICLE,
                    status="extracted",
                    request_id=801,
                ),
            ],
        ),
        aggregation=MultiSourceAggregationOutput(
            session_id=701,
            correlation_id="cid-agg-audit",
            status="completed",
            source_type="web_article",
            total_items=1,
            extracted_items=1,
            used_source_count=1,
            overview="Audited aggregation",
            source_coverage=[
                SourceCoverageEntry(
                    position=0,
                    item_id=9001,
                    source_item_id="src_audit",
                    source_kind=SourceKind.WEB_ARTICLE,
                    status="extracted",
                    used_in_summary=True,
                ),
            ],
        ),
    )

    aggregate_mock = AsyncMock(return_value=fake_result)
    audit_mock = MagicMock()
    runtime = _set_runtime(client, db)
    try:
        with (
            patch(
                "app.application.services.multi_source_aggregation_service.MultiSourceAggregationService.aggregate",
                new=aggregate_mock,
            ),
            patch(
                "app.api.routers.aggregation.build_async_audit_sink",
                return_value=audit_mock,
            ),
            _allow_public_urls(),
        ):
            response = client.post(
                "/v1/aggregations",
                headers=_auth_headers(user_id, client_id="cli-audit-v1"),
                json={
                    "items": [
                        {"url": "https://example.com/article"},
                    ],
                    "metadata": {"submitted_by": "test"},
                },
            )
    finally:
        client.app.state.runtime = runtime

    assert response.status_code == 200
    aggregate_kwargs = aggregate_mock.await_args.kwargs
    assert aggregate_kwargs["metadata"]["entrypoint"] == "api"
    assert aggregate_kwargs["metadata"]["client_id"] == "cli-audit-v1"
    assert aggregate_kwargs["metadata"]["submitted_by"] == "test"
    assert [call.args[1] for call in audit_mock.call_args_list] == [
        "aggregation.bundle_create_requested",
        "aggregation.bundle_create_succeeded",
    ]
    assert audit_mock.call_args_list[0].args[2]["client_id"] == "cli-audit-v1"
    assert audit_mock.call_args_list[1].args[2]["session_id"] == 701


def test_create_aggregation_bundle_endpoint_accepts_single_item(client, db, user_factory):
    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424242
    user_factory(username="aggregation_api_single_user", telegram_user_id=user_id)

    fake_result = MultiSourceAggregationRunResult(
        extraction=MultiSourceExtractionOutput(
            session_id=78,
            correlation_id="cid-agg-single",
            status="completed",
            successful_count=1,
            failed_count=0,
            duplicate_count=0,
            items=[
                SourceExtractionItemResult(
                    position=0,
                    item_id=1101,
                    source_item_id="src_single",
                    source_kind=SourceKind.WEB_ARTICLE,
                    status="extracted",
                    request_id=601,
                ),
            ],
        ),
        aggregation=MultiSourceAggregationOutput(
            session_id=78,
            correlation_id="cid-agg-single",
            status="completed",
            source_type="web_article",
            total_items=1,
            extracted_items=1,
            used_source_count=1,
            overview="Single-source synthesis",
            source_coverage=[
                SourceCoverageEntry(
                    position=0,
                    item_id=1101,
                    source_item_id="src_single",
                    source_kind=SourceKind.WEB_ARTICLE,
                    status="extracted",
                    used_in_summary=True,
                ),
            ],
        ),
    )

    runtime = _set_runtime(client, db)
    try:
        with (
            patch(
                "app.application.services.multi_source_aggregation_service.MultiSourceAggregationService.aggregate",
                new=AsyncMock(return_value=fake_result),
            ),
            _allow_public_urls(),
        ):
            response = client.post(
                "/v1/aggregations",
                headers=_auth_headers(user_id),
                json={
                    "items": [
                        {"url": "https://example.com/article"},
                    ],
                    "lang_preference": "en",
                },
            )
    finally:
        client.app.state.runtime = runtime

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["session"]["sessionId"] == 78
    assert payload["data"]["session"]["successfulCount"] == 1
    assert payload["data"]["aggregation"]["source_type"] == "web_article"
    assert [item["sourceKind"] for item in payload["data"]["items"]] == ["web_article"]


def test_create_aggregation_bundle_endpoint_surfaces_processing_error_code(
    client, db, user_factory
):
    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424242
    user_factory(username="aggregation_api_error_user", telegram_user_id=user_id)

    runtime = _set_runtime(client, db)
    try:
        with (
            patch(
                "app.application.services.multi_source_aggregation_service.MultiSourceAggregationService.aggregate",
                new=AsyncMock(
                    side_effect=RuntimeError("No source extractions completed successfully")
                ),
            ),
            patch("app.api.routers.aggregation.record_request") as metrics_mock,
            _allow_public_urls(),
        ):
            response = client.post(
                "/v1/aggregations",
                headers=_auth_headers(user_id, client_id="cli-agg-error-v1"),
                json={
                    "items": [
                        {"url": "https://example.com/article"},
                    ],
                    "lang_preference": "en",
                },
            )
    finally:
        client.app.state.runtime = runtime

    assert response.status_code == 500
    payload = response.json()
    assert payload["error"]["code"] == "PROCESSING_ERROR"
    assert payload["error"]["details"]["reason_code"] == "AGGREGATION_UPSTREAM_FAILURE"
    assert (
        payload["error"]["details"]["upstream_error"]
        == "No source extractions completed successfully"
    )
    metric_kwargs = metrics_mock.call_args.kwargs
    assert metric_kwargs["request_type"] == "aggregation.create"
    assert metric_kwargs["status"] == "error"
    assert metric_kwargs["source"] == "cli"


def test_create_aggregation_bundle_endpoint_rejects_blocked_ssrf_url(client, db, user_factory):
    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424242
    user_factory(username="aggregation_api_ssrf_user", telegram_user_id=user_id)

    aggregate_mock = AsyncMock()
    audit_mock = MagicMock()
    runtime = _set_runtime(client, db)
    try:
        with (
            patch(
                "app.application.services.multi_source_aggregation_service.MultiSourceAggregationService.aggregate",
                new=aggregate_mock,
            ),
            patch(
                "app.api.routers.aggregation.build_async_audit_sink",
                return_value=audit_mock,
            ),
        ):
            response = client.post(
                "/v1/aggregations",
                headers=_auth_headers(user_id, client_id="cli-ssrf-v1"),
                json={"items": [{"url": "http://localhost/internal"}]},
            )
    finally:
        client.app.state.runtime = runtime

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert payload["error"]["details"]["reason"] == "Localhost is not allowed"
    assert aggregate_mock.await_count == 0
    assert [call.args[1] for call in audit_mock.call_args_list] == [
        "aggregation.bundle_create_requested",
        "aggregation.bundle_create_blocked_ssrf",
    ]


def test_get_aggregation_bundle_endpoint_returns_persisted_session(client, db, user_factory):
    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424242
    user_factory(username="aggregation_lookup_user", telegram_user_id=user_id)

    repo = build_aggregation_session_repository(db)
    session_id = asyncio.run(
        repo.async_create_aggregation_session(
            user_id=user_id,
            correlation_id="cid-agg-fetch",
            total_items=2,
            bundle_metadata={"entrypoint": "api"},
        )
    )
    first_source = SourceItem.create(
        kind=SourceKind.WEB_ARTICLE,
        original_value="https://example.com/a",
    )
    second_source = SourceItem.create(
        kind=SourceKind.X_POST,
        original_value="https://x.com/example/status/1",
    )
    asyncio.run(repo.async_add_aggregation_session_item(session_id, first_source, 0))
    asyncio.run(repo.async_add_aggregation_session_item(session_id, second_source, 1))
    asyncio.run(
        repo.async_update_aggregation_session_output(
            session_id,
            {
                "session_id": session_id,
                "correlation_id": "cid-agg-fetch",
                "status": "completed",
                "source_type": "mixed",
                "total_items": 2,
                "extracted_items": 2,
                "used_source_count": 2,
                "overview": "Persisted synthesis output",
            },
        )
    )
    asyncio.run(
        repo.async_update_aggregation_session_status(
            session_id,
            status=AggregationSessionStatus.PROCESSING,
        )
    )
    asyncio.run(
        repo.async_update_aggregation_session_status(
            session_id,
            status=AggregationSessionStatus.COMPLETED,
        )
    )
    runtime = getattr(client.app.state, "runtime", None)
    client.app.state.runtime = SimpleNamespace(db=db)
    try:
        response = client.get(
            f"/v1/aggregations/{session_id}",
            headers=_auth_headers(user_id),
        )
    finally:
        client.app.state.runtime = runtime

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["session"]["id"] == session_id
    assert payload["data"]["session"]["correlation_id"] == "cid-agg-fetch"
    assert payload["data"]["session"]["started_at"] is not None
    assert payload["data"]["session"]["completed_at"] is not None
    assert payload["data"]["session"]["progress"]["completionPercent"] == 0
    assert payload["data"]["aggregation"]["overview"] == "Persisted synthesis output"
    assert [item["source_kind"] for item in payload["data"]["items"]] == [
        "web_article",
        "x_post",
    ]


def test_get_aggregation_bundle_endpoint_rejects_foreign_session_and_records_metric(
    client, db, user_factory
):
    allowed_ids = Config.get_allowed_user_ids()
    owner_user_id = int(allowed_ids[0]) if allowed_ids else 424242
    foreign_user_id = int(allowed_ids[1]) if len(allowed_ids) > 1 else owner_user_id + 1
    user_factory(username="aggregation_owner_user", telegram_user_id=owner_user_id)
    user_factory(username="aggregation_foreign_user", telegram_user_id=foreign_user_id)

    repo = build_aggregation_session_repository(db)
    session_id = asyncio.run(
        repo.async_create_aggregation_session(
            user_id=owner_user_id,
            correlation_id="cid-agg-foreign",
            total_items=1,
        )
    )

    runtime = getattr(client.app.state, "runtime", None)
    client.app.state.runtime = SimpleNamespace(db=db)
    try:
        with (
            patch("app.api.routers.auth.dependencies.Config.is_user_allowed", return_value=True),
            patch("app.api.routers.aggregation.record_request") as metrics_mock,
        ):
            response = client.get(
                f"/v1/aggregations/{session_id}",
                headers=_auth_headers(foreign_user_id, client_id="cli-foreign-v1"),
            )
    finally:
        client.app.state.runtime = runtime

    assert response.status_code == 403
    payload = response.json()
    assert payload["error"]["code"] == "FORBIDDEN"
    metrics_mock.assert_called_once()
    metric_kwargs = metrics_mock.call_args.kwargs
    assert metric_kwargs["request_type"] == "aggregation.get"
    assert metric_kwargs["status"] == "error"
    assert metric_kwargs["source"] == "cli"
    assert metric_kwargs["latency_seconds"] >= 0


def test_delete_aggregation_bundle_endpoint_removes_owned_session_and_items(
    client, db, user_factory
):
    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424242
    user_factory(username="aggregation_delete_user", telegram_user_id=user_id)

    repo = build_aggregation_session_repository(db)
    session_id = asyncio.run(
        repo.async_create_aggregation_session(
            user_id=user_id,
            correlation_id="cid-agg-delete",
            total_items=1,
        )
    )
    source = SourceItem.create(
        kind=SourceKind.WEB_ARTICLE,
        original_value="https://example.com/delete-me",
    )
    asyncio.run(repo.async_add_aggregation_session_item(session_id, source, 0))

    runtime = _set_runtime(client, db)
    try:
        with patch("app.api.routers.aggregation.record_request") as metrics_mock:
            response = client.delete(
                f"/v1/aggregations/{session_id}",
                headers=_auth_headers(user_id, client_id="cli-delete-v1"),
            )
    finally:
        client.app.state.runtime = runtime

    assert response.status_code == 204
    assert response.content == b""
    assert asyncio.run(repo.async_get_aggregation_session(session_id)) is None
    assert asyncio.run(repo.async_get_aggregation_session_items(session_id)) == []
    metrics_mock.assert_called_once()
    metric_kwargs = metrics_mock.call_args.kwargs
    assert metric_kwargs["request_type"] == "aggregation.delete"
    assert metric_kwargs["status"] == "success"
    assert metric_kwargs["source"] == "cli"
    assert metric_kwargs["latency_seconds"] >= 0


def test_delete_aggregation_bundle_endpoint_returns_not_found_for_foreign_session(
    client, db, user_factory
):
    allowed_ids = Config.get_allowed_user_ids()
    owner_user_id = int(allowed_ids[0]) if allowed_ids else 424242
    foreign_user_id = int(allowed_ids[1]) if len(allowed_ids) > 1 else owner_user_id + 1
    user_factory(username="aggregation_delete_owner", telegram_user_id=owner_user_id)
    user_factory(username="aggregation_delete_foreign", telegram_user_id=foreign_user_id)

    repo = build_aggregation_session_repository(db)
    session_id = asyncio.run(
        repo.async_create_aggregation_session(
            user_id=owner_user_id,
            correlation_id="cid-agg-delete-foreign",
            total_items=1,
        )
    )

    runtime = _set_runtime(client, db)
    try:
        with (
            patch("app.api.routers.auth.dependencies.Config.is_user_allowed", return_value=True),
            patch("app.api.routers.aggregation.record_request") as metrics_mock,
        ):
            response = client.delete(
                f"/v1/aggregations/{session_id}",
                headers=_auth_headers(foreign_user_id, client_id="cli-foreign-v1"),
            )
    finally:
        client.app.state.runtime = runtime

    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "NOT_FOUND"
    assert asyncio.run(repo.async_get_aggregation_session(session_id)) is not None
    metrics_mock.assert_called_once()
    metric_kwargs = metrics_mock.call_args.kwargs
    assert metric_kwargs["request_type"] == "aggregation.delete"
    assert metric_kwargs["status"] == "error"
    assert metric_kwargs["source"] == "cli"
    assert metric_kwargs["latency_seconds"] >= 0


def test_list_aggregation_bundles_endpoint_returns_only_authenticated_user_sessions(
    client, db, user_factory
):
    allowed_ids = Config.get_allowed_user_ids()
    primary_user_id = int(allowed_ids[0]) if allowed_ids else 424242
    secondary_user_id = primary_user_id + 1
    user_factory(username="aggregation_list_primary", telegram_user_id=primary_user_id)
    user_factory(username="aggregation_list_secondary", telegram_user_id=secondary_user_id)

    repo = build_aggregation_session_repository(db)
    first_session_id = asyncio.run(
        repo.async_create_aggregation_session(
            user_id=primary_user_id,
            correlation_id="cid-agg-list-1",
            total_items=3,
        )
    )
    asyncio.run(
        repo.async_update_aggregation_session_counts(
            first_session_id,
            successful_count=2,
            failed_count=1,
            duplicate_count=0,
        )
    )
    asyncio.run(
        repo.async_update_aggregation_session_status(
            first_session_id,
            status=AggregationSessionStatus.PARTIAL,
        )
    )
    asyncio.run(
        repo.async_create_aggregation_session(
            user_id=secondary_user_id,
            correlation_id="cid-agg-list-2",
            total_items=1,
        )
    )

    runtime = getattr(client.app.state, "runtime", None)
    client.app.state.runtime = SimpleNamespace(db=db)
    try:
        response = client.get(
            "/v1/aggregations?limit=20&offset=0",
            headers=_auth_headers(primary_user_id),
        )
    finally:
        client.app.state.runtime = runtime

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert [session["id"] for session in payload["data"]["sessions"]] == [first_session_id]
    assert payload["data"]["sessions"][0]["status"] == AggregationSessionStatus.PARTIAL.value
    assert payload["data"]["sessions"][0]["started_at"] is not None
    assert payload["data"]["sessions"][0]["completed_at"] is not None
    assert payload["data"]["sessions"][0]["progress"]["completionPercent"] == 100
    assert payload["meta"]["pagination"]["hasMore"] is False


def test_external_aggregation_request_flow_end_to_end(client, db, user_factory):
    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424242
    user_factory(username="aggregation_e2e_user", telegram_user_id=user_id)

    class FakeExtractor:
        cfg = SimpleNamespace(runtime=SimpleNamespace(aggregation_non_youtube_video_enabled=True))

        async def extract_content_pure(
            self,
            *,
            url: str,
            correlation_id: str,
            request_id: int | None = None,
        ) -> tuple[str, str, dict[str, str | int]]:
            if "x.com" in url:
                return (
                    "Breaking post with concrete details.",
                    "markdown",
                    {
                        "title": "X post",
                        "detected_lang": "en",
                    },
                )
            return (
                "Longer article body with supporting context and examples.",
                "markdown",
                {
                    "title": "Web article",
                    "detected_lang": "en",
                },
            )

    runtime = getattr(client.app.state, "runtime", None)
    client.app.state.runtime = SimpleNamespace(
        cfg=load_config(allow_stub_telegram=True),
        db=db,
        background_processor=SimpleNamespace(
            url_processor=SimpleNamespace(content_extractor=FakeExtractor())
        ),
        core=SimpleNamespace(llm_client=None),
    )

    try:
        with (
            _allow_public_urls(),
            patch("app.api.routers.aggregation.record_request") as metrics_mock,
        ):
            create_response = client.post(
                "/v1/aggregations",
                headers=_auth_headers(user_id, client_id="cli-e2e-v1"),
                json={
                    "items": [
                        {
                            "url": "https://example.com/article",
                            "source_kind_hint": "web_article",
                        },
                        {
                            "url": "https://x.com/example/status/1",
                            "source_kind_hint": "x_post",
                        },
                    ],
                    "lang_preference": "en",
                    "metadata": {"submitted_by": "e2e-test"},
                },
            )
            assert create_response.status_code == 200
            create_payload = create_response.json()
            session_id = create_payload["data"]["session"]["sessionId"]

            get_response = client.get(
                f"/v1/aggregations/{session_id}",
                headers=_auth_headers(user_id, client_id="cli-e2e-v1"),
            )
            list_response = client.get(
                "/v1/aggregations?limit=20&offset=0",
                headers=_auth_headers(user_id, client_id="cli-e2e-v1"),
            )
    finally:
        client.app.state.runtime = runtime

    assert get_response.status_code == 200
    assert list_response.status_code == 200

    get_payload = get_response.json()
    list_payload = list_response.json()

    assert create_payload["data"]["session"]["status"] == "completed"
    assert create_payload["data"]["session"]["progress"]["completionPercent"] == 100
    assert create_payload["data"]["aggregation"]["source_type"] == "mixed"
    assert create_payload["data"]["aggregation"]["overview"]
    assert create_payload["data"]["sourceBundle"]["bundleId"] == session_id
    assert [
        item["extractionStatus"] for item in create_payload["data"]["sourceBundle"]["items"]
    ] == [
        "extracted",
        "extracted",
    ]
    assert all(item["sourceItemId"] for item in create_payload["data"]["sourceBundle"]["items"])
    assert [item["status"] for item in create_payload["data"]["items"]] == [
        "extracted",
        "extracted",
    ]
    assert all(item["requestId"] is None for item in create_payload["data"]["items"])

    assert get_payload["data"]["session"]["id"] == session_id
    assert get_payload["data"]["session"]["status"] == "completed"
    assert get_payload["data"]["session"]["progress"]["successfulCount"] == 2
    assert get_payload["data"]["aggregation"]["metadata"]["generation_mode"] == "heuristic_fallback"
    claim_source_ids = {
        source_item_id
        for claim in get_payload["data"]["aggregation"]["key_claims"]
        for source_item_id in claim["source_item_ids"]
    }
    bundle_source_ids = {
        item["sourceItemId"] for item in get_payload["data"]["sourceBundle"]["items"]
    }
    assert claim_source_ids
    assert claim_source_ids <= bundle_source_ids
    assert len(get_payload["data"]["items"]) == 2

    assert [session["id"] for session in list_payload["data"]["sessions"]] == [session_id]
    assert list_payload["data"]["sessions"][0]["status"] == "completed"

    assert [call.kwargs["request_type"] for call in metrics_mock.call_args_list] == [
        "aggregation.create",
        "aggregation.get",
        "aggregation.list",
    ]
    assert {call.kwargs["source"] for call in metrics_mock.call_args_list} == {"cli"}
    assert {call.kwargs["status"] for call in metrics_mock.call_args_list} == {"success"}
    assert all(call.kwargs["latency_seconds"] >= 0 for call in metrics_mock.call_args_list)


def test_external_aggregation_detail_exposes_failed_source_provenance(client, db, user_factory):
    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424242
    user_factory(username="aggregation_partial_user", telegram_user_id=user_id)

    class FakeExtractor:
        cfg = SimpleNamespace(runtime=SimpleNamespace(aggregation_non_youtube_video_enabled=True))

        async def extract_content_pure(
            self,
            *,
            url: str,
            correlation_id: str,
            request_id: int | None = None,
        ) -> tuple[str, str, dict[str, str]]:
            if "failed.example" in url:
                raise TimeoutError("source timed out")
            return (
                "Successful article body with one source-grounded detail.",
                "markdown",
                {
                    "title": "Working source",
                    "detected_lang": "en",
                    "author": "Reporter",
                    "published_at": "2026-05-20T10:00:00Z",
                },
            )

    runtime = getattr(client.app.state, "runtime", None)
    client.app.state.runtime = SimpleNamespace(
        cfg=load_config(allow_stub_telegram=True),
        db=db,
        background_processor=SimpleNamespace(
            url_processor=SimpleNamespace(content_extractor=FakeExtractor())
        ),
        core=SimpleNamespace(llm_client=None),
    )

    try:
        with _allow_public_urls():
            create_response = client.post(
                "/v1/aggregations",
                headers=_auth_headers(user_id, client_id="cli-partial-v1"),
                json={
                    "items": [
                        {
                            "url": "https://example.com/working",
                            "source_kind_hint": "web_article",
                        },
                        {
                            "url": "https://failed.example/article",
                            "source_kind_hint": "web_article",
                        },
                    ],
                    "lang_preference": "en",
                },
            )
            assert create_response.status_code == 200
            session_id = create_response.json()["data"]["session"]["sessionId"]
            get_response = client.get(
                f"/v1/aggregations/{session_id}",
                headers=_auth_headers(user_id, client_id="cli-partial-v1"),
            )
    finally:
        client.app.state.runtime = runtime

    assert get_response.status_code == 200
    payload = get_response.json()
    source_items = payload["data"]["sourceBundle"]["items"]
    assert [item["extractionStatus"] for item in source_items] == ["extracted", "failed"]
    assert source_items[0]["title"] == "Working source"
    assert source_items[0]["author"] == "Reporter"
    assert source_items[0]["publishedAt"] == "2026-05-20T10:00:00Z"
    assert source_items[1]["originalUrl"] == "https://failed.example/article"
    assert source_items[1]["normalizedUrl"] == "https://failed.example/article"
    assert source_items[1]["errorCode"] == "source_extraction_failed"
    assert "source timed out" in source_items[1]["errorMessage"]
    assert source_items[1]["summaryId"] is None
    coverage_by_id = {
        entry["source_item_id"]: entry
        for entry in payload["data"]["aggregation"]["source_coverage"]
    }
    assert set(coverage_by_id) == {item["sourceItemId"] for item in source_items}
    assert coverage_by_id[source_items[1]["sourceItemId"]]["status"] == "failed"


def test_aggregation_source_item_serializer_hides_deleted_summary_link() -> None:
    from app.api.aggregation_provenance import build_source_bundle, source_item_from_record
    from app.api.models.responses import AggregationDetailResponse

    bundle = build_source_bundle(
        session_id=42,
        correlation_id="cid-source-bundle",
        status="partial",
        persisted_items=[
            {
                "aggregation_session_id": 42,
                "id": 6,
                "position": 0,
                "source_item_id": "src_ok",
                "source_kind": "web_article",
                "status": "extracted",
                "original_value": "https://example.com/ok?utm_source=test",
                "normalized_value": "https://example.com/ok",
                "request_id": 100,
                "crawl_result_id": 200,
                "summary_id": 300,
                "normalized_document_json": {
                    "title": "Stored title",
                    "metadata": {
                        "author": "Reporter",
                        "published_at": "2026-05-20T10:00:00Z",
                    },
                },
            },
            {
                "aggregation_session_id": 42,
                "id": 7,
                "position": 1,
                "source_item_id": "src_failed",
                "source_kind": "web_article",
                "status": "failed",
                "original_value": "https://failed.example/article",
                "normalized_value": "https://failed.example/article",
                "failure_code": "source_extraction_failed",
                "failure_message": "source timed out",
            },
        ],
    ).model_dump(by_alias=True)

    assert bundle["bundleId"] == 42
    assert bundle["status"] == "partial"
    assert [item["extractionStatus"] for item in bundle["items"]] == ["extracted", "failed"]
    assert bundle["items"][0]["sourceItemId"] == "src_ok"
    assert bundle["items"][0]["normalizedUrl"] == "https://example.com/ok"
    assert bundle["items"][0]["title"] == "Stored title"
    assert bundle["items"][0]["domain"] == "example.com"
    assert bundle["items"][0]["author"] == "Reporter"
    assert bundle["items"][0]["publishedAt"] == "2026-05-20T10:00:00Z"
    assert bundle["items"][0]["requestId"] == 100
    assert bundle["items"][0]["crawlResultId"] == 200
    assert bundle["items"][0]["summaryId"] == 300
    assert bundle["items"][1]["sourceItemId"] == "src_failed"
    assert bundle["items"][1]["errorCode"] == "source_extraction_failed"
    assert bundle["items"][1]["errorMessage"] == "source timed out"

    response_payload = {
        "success": True,
        "data": {
            "session": {"id": 42},
            "items": [],
            "aggregation": {"source_coverage": [{"source_item_id": "src_ok"}]},
            "sourceBundle": bundle,
        },
        "meta": {
            "correlation_id": "cid-source-bundle",
            "timestamp": "2026-05-22T00:00:00Z",
            "version": "test",
            "api_version": "1.0.0",
        },
    }
    validated = AggregationDetailResponse.model_validate(response_payload)
    dumped = validated.model_dump(by_alias=True)
    assert dumped["data"]["sourceBundle"]["items"][0]["sourceItemId"] == "src_ok"

    item = source_item_from_record(
        {
            "aggregation_session_id": 42,
            "id": 7,
            "position": 0,
            "source_item_id": "src_deleted",
            "source_kind": "web_article",
            "status": "extracted",
            "original_value": "https://example.com/deleted",
            "normalized_value": "https://example.com/deleted",
            "request_id": 101,
            "crawl_result_id": 202,
            "summary_id": 303,
            "summary_is_deleted": True,
        }
    ).model_dump(by_alias=True)

    assert item["deleted"] is True
    assert item["requestId"] == 101
    assert item["crawlResultId"] == 202
    assert item["summaryId"] is None


def test_create_aggregation_bundle_endpoint_rejects_invalid_source_kind_hint(
    client, db, user_factory
):
    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424242
    user_factory(username="aggregation_invalid_hint_user", telegram_user_id=user_id)

    runtime = _set_runtime(client, db)
    try:
        response = client.post(
            "/v1/aggregations",
            headers=_auth_headers(user_id),
            json={
                "items": [
                    {
                        "url": "https://example.com/article",
                        "source_kind_hint": "unknown_kind",
                    }
                ],
            },
        )
    finally:
        client.app.state.runtime = runtime

    assert response.status_code == 422


def test_create_aggregation_bundle_endpoint_returns_404_when_rollout_disabled(
    client, db, user_factory
):
    from app.api.routers.aggregation import _get_rollout_gate

    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424242
    user_factory(username="aggregation_api_rollout_user", telegram_user_id=user_id)

    async def _evaluate(_: int) -> AggregationRolloutDecision:
        return AggregationRolloutDecision(
            enabled=False,
            reason="Aggregation bundles are currently disabled.",
            stage=AggregationRolloutStage.DISABLED,
        )

    runtime = _set_runtime(client, db)
    client.app.dependency_overrides[_get_rollout_gate] = lambda: SimpleNamespace(evaluate=_evaluate)
    try:
        response = client.post(
            "/v1/aggregations",
            headers=_auth_headers(user_id),
            json={
                "items": [
                    {"url": "https://example.com/article"},
                    {"url": "https://x.com/example/status/1"},
                ]
            },
        )
    finally:
        client.app.dependency_overrides.pop(_get_rollout_gate, None)
        client.app.state.runtime = runtime

    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "NOT_FOUND"
