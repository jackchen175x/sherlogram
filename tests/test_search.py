import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

import pytest
from telethon import functions, types

from tg_mcp import search


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


# ---------- search_public_posts ----------


@pytest.mark.asyncio
async def test_public_post_search_requires_exactly_one_search_mode(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(search, "client", fake)

    neither = json.loads(await search.search_public_posts())
    both = json.loads(await search.search_public_posts(query="GEO", hashtag="#GEO"))

    assert neither["status"] == "invalid_request"
    assert both["status"] == "invalid_request"
    assert fake.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset_rate": -1},
        {"offset_id": 5},  # 翻页却没给 offset_peer
    ],
)
async def test_public_post_search_rejects_bad_paging_params(monkeypatch, kwargs):
    fake = _FakeClient()
    monkeypatch.setattr(search, "client", fake)

    payload = json.loads(await search.search_public_posts(query="GEO", **kwargs))

    assert payload["status"] == "invalid_request"
    assert fake.requests == []


@pytest.mark.asyncio
async def test_full_text_public_post_search_checks_quota_and_never_spends_stars(monkeypatch):
    fake = _FakeClient(remains=10)
    monkeypatch.setattr(search, "client", fake)

    payload = json.loads(await search.search_public_posts(query="GEO tools", limit=20))

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
    monkeypatch.setattr(search, "client", fake)

    payload = json.loads(await search.search_public_posts(query="GEO tools"))

    assert payload["status"] == "quota_exhausted"
    assert payload["stars_spent"] == 0
    assert payload["quota"]["stars_required"] == 1
    assert len(fake.requests) == 1
    assert isinstance(fake.requests[0], functions.channels.CheckSearchPostsFloodRequest)


@pytest.mark.asyncio
async def test_cached_query_can_run_when_daily_quota_is_exhausted(monkeypatch):
    fake = _FakeClient(remains=0, query_is_free=True)
    monkeypatch.setattr(search, "client", fake)

    payload = json.loads(await search.search_public_posts(query="cached GEO query"))

    assert payload["status"] == "ok"
    assert len(fake.requests) == 2


@pytest.mark.asyncio
async def test_hashtag_search_skips_full_text_quota_check(monkeypatch):
    fake = _FakeClient(remains=0)
    monkeypatch.setattr(search, "client", fake)

    payload = json.loads(await search.search_public_posts(hashtag="#GEO"))

    assert payload["status"] == "ok"
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert isinstance(request, functions.channels.SearchPostsRequest)
    assert request.hashtag == "GEO"
    assert request.query is None
    assert request.allow_paid_stars is None


@pytest.mark.asyncio
async def test_next_page_skips_quota_check_and_resolves_offset_peer(monkeypatch):
    fake = _FakeClient(remains=0)  # 额度为 0：翻页不该去查额度，也不该被拦
    monkeypatch.setattr(search, "client", fake)
    resolved = []

    async def fake_resolve_input_entity(ref):
        resolved.append(ref)
        return types.InputPeerChannel(channel_id=123, access_hash=456)

    monkeypatch.setattr(search, "resolve_input_entity", fake_resolve_input_entity)

    payload = json.loads(
        await search.search_public_posts(
            query="GEO tools", offset_rate=777, offset_peer="@geo_research", offset_id=42
        )
    )

    assert payload["status"] == "ok"
    assert resolved == ["@geo_research"]
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert isinstance(request, functions.channels.SearchPostsRequest)
    assert (request.offset_rate, request.offset_id) == (777, 42)
    assert isinstance(request.offset_peer, types.InputPeerChannel)


# ---------- search_public_chats ----------


@pytest.mark.asyncio
async def test_search_public_chats_lists_chats_and_users(monkeypatch):
    class Fake:
        requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace(
                chats=[SimpleNamespace(id=1, title="GEO Group", username="geo_group")],
                users=[SimpleNamespace(id=2, first_name="Geo", last_name=None, username="geo_bot")],
            )

    fake = Fake()
    monkeypatch.setattr(search, "client", fake)

    payload = json.loads(await search.search_public_chats("geo", limit=5))

    assert [p["id"] for p in payload] == [1, 2]
    assert payload[0]["username"] == "geo_group"
    assert payload[1]["name"] == "Geo"
    assert isinstance(fake.requests[0], functions.contacts.SearchRequest)
    assert (fake.requests[0].q, fake.requests[0].limit) == ("geo", 5)


# ---------- search_global ----------


def _global_msg(msg_id, text):
    chat = SimpleNamespace(title="Market", username="market_chat")
    return SimpleNamespace(
        id=msg_id,
        chat=chat,
        chat_id=-100,
        sender=SimpleNamespace(first_name="Seller", last_name=None, username="seller_x"),
        date=datetime(2026, 8, 5, tzinfo=timezone.utc),
        message=text,
        fwd_from=None,
        edit_date=None,
        via_bot_id=None,
    )


@pytest.mark.asyncio
async def test_search_global_pages_and_appends_clues(monkeypatch):
    calls = []

    class Fake:
        async def get_messages(self, entity, limit=None, search=None, add_offset=0):
            calls.append((entity, limit, search, add_offset))
            return [_global_msg(1, "buy via @shopbot_x"), _global_msg(2, "also @shopbot_x " + "x" * 500)]

    monkeypatch.setattr(search, "client", Fake())

    out = await search.search_global("gpt", page=3, page_size=2)

    assert calls == [(None, 2, "gpt", 4)]
    assert "Chat: Market (@market_chat) | ID: 1 | Seller (@seller_x)" in out
    assert "已截断" in out  # 超过 400 字的那条被截断并明说
    assert "shopbot_x×2" in out  # 高频 @账号 被附在末尾当线索


@pytest.mark.asyncio
async def test_search_global_empty_page(monkeypatch):
    class Fake:
        async def get_messages(self, *args, **kwargs):
            return []

    monkeypatch.setattr(search, "client", Fake())

    assert await search.search_global("nothing") == "No messages found for this page."


# ---------- search_gifs ----------


@pytest.mark.asyncio
async def test_search_gifs_returns_document_ids_and_caches_docs(monkeypatch):
    doc = SimpleNamespace(id=555)

    class Fake:
        requests = []

        async def get_input_entity(self, target):
            return SimpleNamespace(target=target)

        async def __call__(self, request):
            self.requests.append(request)
            # 没有 document 的结果（比如纯文字答案）要被跳过
            return SimpleNamespace(results=[SimpleNamespace(document=doc), SimpleNamespace(document=None)])

    fake = Fake()
    monkeypatch.setattr(search, "client", fake)
    search.GIF_DOCS.clear()

    assert json.loads(await search.search_gifs("cat", limit=3)) == [555]
    request = fake.requests[0]
    assert isinstance(request, functions.messages.GetInlineBotResultsRequest)
    assert request.query == "cat"
    assert search.GIF_DOCS[555] is doc  # send_message(gif_id=555) 靠这个缓存拿到完整文档


@pytest.mark.asyncio
async def test_search_gifs_no_results(monkeypatch):
    class Fake:
        async def get_input_entity(self, target):
            return SimpleNamespace(target=target)

        async def __call__(self, request):
            return SimpleNamespace(results=[])

    monkeypatch.setattr(search, "client", Fake())

    assert await search.search_gifs("zzz") == "[]"


# ---------- 对外小件 ----------


def test_extract_public_post_clues_is_public_name():
    clues = search.extract_public_post_clues(
        [{"text": "#a @someone https://x.com/p"}, {"text": "#a @someone"}]
    )
    assert clues["hashtags"] == [{"value": "a", "count": 2}]
    assert clues["mentions"] == [{"value": "someone", "count": 2}]
    assert clues["linked_domains"] == [{"value": "x.com", "count": 1}]
