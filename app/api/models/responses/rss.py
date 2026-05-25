"""RSS feed API response models."""

from __future__ import annotations

from pydantic import BaseModel

from app.api.models.responses.common import SuccessResponse


class RssFeedSubscription(BaseModel):
    subscription_id: int
    feed_id: int | None = None
    feed_title: str | None = None
    feed_url: str | None = None
    site_url: str | None = None
    category_name: str | None = None
    is_active: bool = True
    created_at: str | None = None


class RssFeedsListData(BaseModel):
    feeds: list[RssFeedSubscription]


class RssSubscribeData(BaseModel):
    subscription_id: int
    feed_id: int
    feed_title: str | None = None
    feed_url: str


class RssDeleteData(BaseModel):
    deleted: bool
    id: int


class RssFeedItem(BaseModel):
    id: int
    guid: str | None = None
    title: str | None = None
    url: str | None = None
    author: str | None = None
    published_at: str | None = None
    created_at: str | None = None


class RssFeedItemsData(BaseModel):
    feed_id: int
    items: list[RssFeedItem]


class RssRefreshData(BaseModel):
    feed_id: int
    new_items: int
    not_modified: bool | None = None


class RssImportData(BaseModel):
    imported: int
    errors: int
    total: int


class RssFeedsListSuccessResponse(SuccessResponse):
    data: RssFeedsListData


class RssSubscribeSuccessResponse(SuccessResponse):
    data: RssSubscribeData


class RssDeleteSuccessResponse(SuccessResponse):
    data: RssDeleteData


class RssFeedItemsSuccessResponse(SuccessResponse):
    data: RssFeedItemsData


class RssRefreshSuccessResponse(SuccessResponse):
    data: RssRefreshData


class RssImportSuccessResponse(SuccessResponse):
    data: RssImportData
