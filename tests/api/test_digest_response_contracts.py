"""OpenAPI response contract coverage for digest endpoints."""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.api.routers import custom_digests
from app.api.routers.social import digest


def _routes_by_name(router: object) -> dict[str, APIRoute]:
    return {
        route.name: route
        for route in router.routes  # type: ignore[attr-defined]
        if isinstance(route, APIRoute)
    }


def test_digest_json_endpoints_have_typed_success_envelopes() -> None:
    routes = _routes_by_name(digest.router)
    expected_models = {
        "list_channels": "ChannelSubscriptionsSuccessResponse",
        "subscribe_channel": "ChannelMutationSuccessResponse",
        "unsubscribe_channel": "ChannelMutationSuccessResponse",
        "resolve_channel": "ResolveChannelSuccessResponse",
        "update_channel_controls": "ChannelMutationSuccessResponse",
        "retry_channel": "ChannelMutationSuccessResponse",
        "list_channel_posts": "ChannelPostsSuccessResponse",
        "bulk_unsubscribe": "BulkOperationSuccessResponse",
        "bulk_assign_category": "BulkOperationSuccessResponse",
        "get_preferences": "DigestPreferenceSuccessResponse",
        "update_preferences": "DigestPreferenceSuccessResponse",
        "list_email_addresses": "EmailAddressListSuccessResponse",
        "request_email_verification": "EmailVerificationRequestSuccessResponse",
        "verify_email_address": "EmailVerificationSuccessResponse",
        "list_history": "DigestDeliveryListSuccessResponse",
        "trigger_digest": "TriggerDigestSuccessResponse",
        "trigger_channel_digest": "TriggerChannelDigestSuccessResponse",
        "list_categories": "CategoryListSuccessResponse",
        "create_category": "CategorySuccessResponse",
        "update_category": "CategorySuccessResponse",
        "delete_category": "StatusSuccessResponse",
        "assign_category": "StatusSuccessResponse",
    }

    for route_name, expected_model in expected_models.items():
        response_model = routes[route_name].response_model
        assert response_model is not None
        assert response_model.__name__ == expected_model
        data_schema = response_model.model_json_schema()["properties"]["data"]
        assert data_schema != {}


def test_digest_run_stream_is_documented_as_sse() -> None:
    route = _routes_by_name(digest.router)["stream_digest_run"]

    assert route.response_model is None
    assert set(route.responses[200]["content"]) == {"text/event-stream"}


def test_custom_digest_endpoints_have_typed_success_envelopes() -> None:
    routes = _routes_by_name(custom_digests.router)
    expected_models = {
        "create_custom_digest": "CustomDigestSuccessResponse",
        "list_custom_digests": "CustomDigestListSuccessResponse",
        "get_custom_digest": "CustomDigestSuccessResponse",
        "email_custom_digest": "EmailDeliverySuccessResponse",
    }

    for route_name, expected_model in expected_models.items():
        response_model = routes[route_name].response_model
        assert response_model is not None
        assert response_model.__name__ == expected_model
        data_schema = response_model.model_json_schema()["properties"]["data"]
        assert data_schema != {}
