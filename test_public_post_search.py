import json
import os
from datetime import datetime, timezone

import pytest
from telethon import functions, types

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

import main


def _search_result(remains=9):
    message = types.Message(
        id=42,
        peer_id=types.PeerChannel(123),
        date=datetime(2026, 8, 5, 4, 0, tzinfo=timezone.utc),
        message=(
            "A public GEO research post #AEO by @geoexpert "
            "https://example.com/report"
        ),
        post=True,
        views=1200,
        forwards=15,
    )
    channel = types.Channel(
        id=123,
        title="GEO Research",
        photo=types.ChatPhotoEmpty(),
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        broadcast=True,
        access_hash=456,
        username="geo_research",
    )
    return types.messages.MessagesSlice(
        count=1,
        messages=[message],
        topics=[],
        chats=[channel],
        users=[],
        next_rate=777,
        search_flood=types.SearchPostsFlood(
            total_daily=10,
            remains=remains,
            stars_amount=1,
        ),
    )


class _FakeClient:
    def __init__(self, *, remains=10, query_is_free=False):
        self.remains = remains
        self.query_is_free = query_is_free
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.channels.CheckSearchPostsFloodRequest):
            return types.SearchPostsFlood(
                total_daily=10,
                remains=self.remains,
                stars_amount=1,
                query_is_free=self.query_is_free,
            )
        if isinstance(request, functions.channels.SearchPostsRequest):
            return _search_result(remains=max(0, self.remains - 1))
        raise AssertionError(f"Unexpected request: {type(request).__name__}")


@pytest.mark.asyncio
async def test_public_post_search_requires_exactly_one_search_mode(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(main, "client", fake)

    neither = json.loads(await main.search_public_posts())
    both = json.loads(await main.search_public_posts(query="GEO", hashtag="#GEO"))

    assert neither["status"] == "invalid_request"
    assert both["status"] == "invalid_request"
    assert fake.requests == []


@pytest.mark.asyncio
async def test_full_text_public_post_search_checks_quota_and_never_spends_stars(monkeypatch):
    fake = _FakeClient(remains=10)
    monkeypatch.setattr(main, "client", fake)

    payload = json.loads(await main.search_public_posts(query="GEO tools", limit=20))

    assert payload["status"] == "ok"
    assert payload["stars_spent"] == 0
    assert payload["posts"][0]["url"] == "https://t.me/geo_research/42"
    assert payload["posts"][0]["channel"]["title"] == "GEO Research"
    assert payload["clues"]["hashtags"] == [{"value": "aeo", "count": 1}]
    assert payload["clues"]["mentions"] == [{"value": "geoexpert", "count": 1}]
    assert payload["clues"]["linked_domains"] == [
        {"value": "example.com", "count": 1}
    ]
    assert payload["clues"]["channels"][0]["username"] == "geo_research"
    assert payload["next_offset"] == {
        "offset_rate": 777,
        "offset_peer": "@geo_research",
        "offset_id": 42,
    }
    assert isinstance(fake.requests[0], functions.channels.CheckSearchPostsFloodRequest)
    assert isinstance(fake.requests[1], functions.channels.SearchPostsRequest)
    assert fake.requests[1].allow_paid_stars is None


@pytest.mark.asyncio
async def test_exhausted_quota_stops_before_search_and_reports_no_spend(monkeypatch):
    fake = _FakeClient(remains=0)
    monkeypatch.setattr(main, "client", fake)

    payload = json.loads(await main.search_public_posts(query="GEO tools"))

    assert payload["status"] == "quota_exhausted"
    assert payload["stars_spent"] == 0
    assert payload["quota"]["stars_required"] == 1
    assert len(fake.requests) == 1
    assert isinstance(fake.requests[0], functions.channels.CheckSearchPostsFloodRequest)


@pytest.mark.asyncio
async def test_cached_query_can_run_when_daily_quota_is_exhausted(monkeypatch):
    fake = _FakeClient(remains=0, query_is_free=True)
    monkeypatch.setattr(main, "client", fake)

    payload = json.loads(await main.search_public_posts(query="cached GEO query"))

    assert payload["status"] == "ok"
    assert len(fake.requests) == 2


@pytest.mark.asyncio
async def test_hashtag_search_skips_full_text_quota_check(monkeypatch):
    fake = _FakeClient(remains=0)
    monkeypatch.setattr(main, "client", fake)

    payload = json.loads(await main.search_public_posts(hashtag="#GEO"))

    assert payload["status"] == "ok"
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert isinstance(request, functions.channels.SearchPostsRequest)
    assert request.hashtag == "GEO"
    assert request.query is None
    assert request.allow_paid_stars is None
