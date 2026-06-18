"""Tests for API request Pydantic models."""

import pytest
from pydantic import ValidationError

from app.api.models.requests import (
    AggregationBundleItemRequest,
    AttachTagsRequest,
    CreateGoalRequest,
    CreateRuleRequest,
    CreateTagRequest,
    CreateWebhookRequest,
    ImportOptionsRequest,
    MergeTagsRequest,
    QuickSaveRequest,
    SubmitURLRequest,
    SyncApplyItem,
    SyncApplyRequest,
    TestRuleRequest,
    UpdateRuleRequest,
    UpdateTagRequest,
    UpdateWebhookRequest,
)


class TestCreateTagRequest:
    def test_valid(self):
        req = CreateTagRequest(name="python")
        assert req.name == "python"

    def test_with_color(self):
        req = CreateTagRequest(name="test", color="#FF0000")
        assert req.color == "#FF0000"

    def test_color_defaults_to_none(self):
        req = CreateTagRequest(name="tag")
        assert req.color is None


class TestUpdateTagRequest:
    def test_name_only(self):
        req = UpdateTagRequest(name="renamed")
        assert req.name == "renamed"
        assert req.color is None

    def test_color_only(self):
        req = UpdateTagRequest(color="#00FF00")
        assert req.name is None
        assert req.color == "#00FF00"

    def test_empty_is_valid(self):
        req = UpdateTagRequest()
        assert req.name is None
        assert req.color is None


class TestMergeTagsRequest:
    def test_valid(self):
        req = MergeTagsRequest(source_tag_ids=[1, 2], target_tag_id=3)
        assert len(req.source_tag_ids) == 2
        assert req.target_tag_id == 3

    def test_single_source(self):
        req = MergeTagsRequest(source_tag_ids=[5], target_tag_id=10)
        assert req.source_tag_ids == [5]


class TestAttachTagsRequest:
    def test_with_ids(self):
        req = AttachTagsRequest(tag_ids=[1, 2])
        assert req.tag_ids == [1, 2]

    def test_with_names(self):
        req = AttachTagsRequest(tag_names=["python", "ai"])
        assert req.tag_names == ["python", "ai"]

    def test_both_none_by_default(self):
        req = AttachTagsRequest()
        assert req.tag_ids is None
        assert req.tag_names is None


class TestCreateWebhookRequest:
    def test_valid(self):
        req = CreateWebhookRequest(url="https://example.com/hook", events=["summary.created"])
        assert req.url == "https://example.com/hook"

    def test_with_name(self):
        req = CreateWebhookRequest(
            name="My Hook", url="https://test.com", events=["summary.created"]
        )
        assert req.name == "My Hook"

    def test_name_defaults_to_none(self):
        req = CreateWebhookRequest(url="https://test.com", events=["a"])
        assert req.name is None


class TestUpdateWebhookRequest:
    def test_partial_update(self):
        req = UpdateWebhookRequest(enabled=False)
        assert req.enabled is False
        assert req.url is None
        assert req.events is None

    def test_all_fields(self):
        req = UpdateWebhookRequest(
            name="Updated", url="https://new.com", events=["e1"], enabled=True
        )
        assert req.name == "Updated"
        assert req.url == "https://new.com"


class TestCreateRuleRequest:
    def test_valid(self):
        req = CreateRuleRequest(
            name="Test Rule",
            event_type="summary.created",
            conditions=[{"type": "domain_matches", "operator": "contains", "value": "test"}],
            actions=[{"type": "add_tag", "params": {"tag_name": "auto"}}],
        )
        assert req.name == "Test Rule"

    def test_default_match_mode(self):
        req = CreateRuleRequest(
            name="R", event_type="summary.created", actions=[{"type": "archive", "params": {}}]
        )
        assert req.match_mode == "all"

    def test_default_priority(self):
        req = CreateRuleRequest(
            name="R", event_type="summary.created", actions=[{"type": "archive", "params": {}}]
        )
        assert req.priority == 0

    def test_conditions_default_to_empty(self):
        req = CreateRuleRequest(
            name="R", event_type="summary.created", actions=[{"type": "archive", "params": {}}]
        )
        assert req.conditions == []

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            CreateRuleRequest(
                name="", event_type="summary.created", actions=[{"type": "a", "params": {}}]
            )

    def test_no_actions_rejected(self):
        with pytest.raises(ValidationError):
            CreateRuleRequest(name="R", event_type="summary.created", actions=[])

    def test_priority_bounds(self):
        with pytest.raises(ValidationError):
            CreateRuleRequest(
                name="R",
                event_type="e",
                actions=[{"type": "a", "params": {}}],
                priority=1001,
            )


class TestUpdateRuleRequest:
    def test_partial_update(self):
        req = UpdateRuleRequest(enabled=False)
        assert req.enabled is False
        assert req.name is None

    def test_all_none_is_valid(self):
        req = UpdateRuleRequest()
        assert req.name is None
        assert req.actions is None


class TestTestRuleRequest:
    def test_valid(self):
        req = TestRuleRequest(summary_id=42)
        assert req.summary_id == 42


class TestImportOptionsRequest:
    def test_defaults(self):
        req = ImportOptionsRequest()
        assert req.summarize is False
        assert req.create_tags is True
        assert req.skip_duplicates is True
        assert req.target_collection_id is None

    def test_override_defaults(self):
        req = ImportOptionsRequest(summarize=True, create_tags=False, skip_duplicates=False)
        assert req.summarize is True
        assert req.create_tags is False
        assert req.skip_duplicates is False


class TestQuickSaveRequest:
    def test_minimal(self):
        req = QuickSaveRequest(url="https://example.com")
        assert req.summarize is True

    def test_with_tags(self):
        req = QuickSaveRequest(url="https://test.com", tag_names=["a", "b"])
        assert len(req.tag_names) == 2

    def test_with_note(self):
        req = QuickSaveRequest(url="https://test.com", selected_text="important text")
        assert req.selected_text == "important text"

    def test_defaults(self):
        req = QuickSaveRequest(url="https://example.com")
        assert req.title is None
        assert req.selected_text is None
        assert req.tag_names == []
        assert req.summarize is True


class TestURLSSRFValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/admin",
            "http://127.0.0.1/admin",
            "http://10.0.0.5/admin",
            "http://172.16.0.5/admin",
            "http://192.168.1.5/admin",
            "http://169.254.169.254/latest/meta-data/",
        ],
    )
    def test_submit_url_request_rejects_private_targets(self, url: str) -> None:
        with pytest.raises(ValidationError):
            SubmitURLRequest.model_validate({"input_url": url})

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "data:text/plain,hello",
            "javascript:alert(1)",
        ],
    )
    def test_submit_url_request_rejects_non_http_schemes(self, url: str) -> None:
        with pytest.raises(ValidationError):
            SubmitURLRequest.model_validate({"input_url": url})

    def test_aggregation_bundle_item_rejects_private_target(self) -> None:
        with pytest.raises(ValidationError):
            AggregationBundleItemRequest.model_validate({"url": "http://10.0.0.5/admin"})

    def test_quick_save_rejects_private_target(self) -> None:
        with pytest.raises(ValidationError):
            QuickSaveRequest(url="http://127.0.0.1/admin")


class TestSyncApplyRequest:
    def test_empty_changes_rejected(self):
        with pytest.raises(ValidationError):
            SyncApplyRequest(session_id="sync-test", changes=[])

    def test_oversized_changes_rejected(self):
        change = SyncApplyItem(
            entity_type="summary",
            id=1,
            action="update",
            last_seen_version=0,
            payload={"is_read": True},
        )

        with pytest.raises(ValidationError):
            SyncApplyRequest(session_id="sync-test", changes=[change] * 501)

    def test_changes_accepts_maximum_batch_size(self):
        change = SyncApplyItem(
            entity_type="summary",
            id=1,
            action="update",
            last_seen_version=0,
            payload={"is_read": True},
        )

        req = SyncApplyRequest(session_id="sync-test", changes=[change] * 500)

        assert len(req.changes) == 500


class TestCreateGoalRequest:
    def test_global_default(self):
        req = CreateGoalRequest(goal_type="daily", target_count=5)
        assert req.scope_type == "global"
        assert req.scope_id is None

    def test_tag_scoped(self):
        req = CreateGoalRequest(goal_type="weekly", target_count=3, scope_type="tag", scope_id=42)
        assert req.scope_type == "tag"
        assert req.scope_id == 42

    def test_collection_scoped(self):
        req = CreateGoalRequest(
            goal_type="monthly", target_count=10, scope_type="collection", scope_id=7
        )
        assert req.scope_type == "collection"

    def test_non_global_requires_scope_id(self):
        with pytest.raises(ValidationError):
            CreateGoalRequest(goal_type="daily", target_count=5, scope_type="tag")

    def test_global_rejects_scope_id(self):
        with pytest.raises(ValidationError):
            CreateGoalRequest(goal_type="daily", target_count=5, scope_type="global", scope_id=1)

    def test_target_count_minimum(self):
        with pytest.raises(ValidationError):
            CreateGoalRequest(goal_type="daily", target_count=0)

    def test_target_count_maximum(self):
        with pytest.raises(ValidationError):
            CreateGoalRequest(goal_type="daily", target_count=1001)
