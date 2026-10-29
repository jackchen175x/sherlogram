"""谁最早：拆搜索词、文案归组、最早一条（转发按原帖算）、扩散顺序、线索首次出现；卡片的「最早被提到」。全部假客户端。"""
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

import pytest

from tg_mcp import first, trace
from tests.test_trace import A, B, C, GROUP, fake, msg  # noqa: F401


def D(m, d):
    return datetime(2026, m, d, tzinfo=timezone.utc)


T = "GPT Plus 代充 120元 卡网 card1.shop 秒发 售后无忧 找 @helper_01"
T2 = "G·PT Plus 代充 120元 卡网 card1.shop 秒发 售后无忧 找 @helper_01 今日有货"  # 夹了间隔号、多了几个字，还是同一条
POSTS = [
    msg(1, T, A, date=D(6, 1)),
    msg(2, T2, B, date=D(7, 1)),
    msg(3, T, C, date=D(6, 15), fwd_from=SimpleNamespace(date=D(5, 20), from_id=None, from_name="源头频道")),
    msg(4, "完全不同的广告 收二手苹果手机 价格面议", C, date=D(6, 2)),
    msg(5, T, B, date=D(8, 1)),
]


@pytest.fixture
def ff(fake, monkeypatch):
    async def public(query, limit):
        return [], None

    async def resolve(ident):
        if ident in (777, "demo_group"):
            return GROUP
        raise ValueError(ident)

    monkeypatch.setattr(first, "client", fake)
    monkeypatch.setattr(first, "_public_posts", public)
    monkeypatch.setattr(first, "resolve_entity", resolve)
    fake.search_msgs = list(POSTS)
    fake.msgs["demo_group"] = list(POSTS)
    return fake


def test_terms_split_clues_and_fragments():
    terms, clues, rest = first.terms_of(T)
    assert clues == ["card1.shop", "@helper_01"] and terms == ["card1.shop", "@helper_01", "GPT Plus 代充 120元 卡网 秒发 售后无忧 找"]
    terms, clues, _ = first.terms_of("shopa.pro")
    assert terms == ["shopa.pro"] and clues == ["shopa.pro"]
    terms, _, _ = first.terms_of("第一行很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长的话术\n第二行短一点的话术\n短")
    assert len(terms[0]) == 30 and terms[0].startswith("第一行很长") and terms[1:] == ["第二行短一点的话术"]  # 超长切到 30 字，太短的丢
    assert first.terms_of("   ")[0] == [""] or first.terms_of("abc")[0] == ["abc"]


def test_shingles_ignore_obfuscation():
    a, b = first.shingles(T), first.shingles(T2)
    assert first.jaccard(a, b) > 0.8 and first.jaccard(a, first.shingles("收二手苹果手机 价格面议")) < 0.1


@pytest.mark.asyncio
async def test_who_first_groups_and_forward_origin(ff):
    out = json.loads(await first.who_first(query=T))
    assert out["scanned"] == 5 and len(out["groups"]) == 2
    g = out["groups"][0]
    assert g["matches_query"] and g["copies"] == 4 and g["senders"] == 3 and g["chats"] == 1 and g["latest"] == "2026-08-01"
    # 最早那条是 C 转发的，按原帖算：源头频道 05-20
    assert g["first"] == {"who": "源头频道", "chat": "交流群", "date": "2026-05-20", "message_id": 3, "via_forward": "@seller_c 在「交流群」转发的，原帖 2026-05-20"}
    assert [(s["who"], s["first"], s["posts"]) for s in g["spread"]] == [("@seller_c", "2026-05-20", 1), ("@seller_a", "2026-06-01", 1), ("@seller_b", "2026-07-01", 2)]
    assert out["groups"][1]["copies"] == 1 and out["groups"][1]["matches_query"] is False
    assert out["first_mention"]["card1.shop"] == {"date": "2026-05-20", "who": "源头频道", "chat": "交流群", "message_id": 3, "by_self": False}
    assert "最早是 源头频道 2026-05-20" in out["summary"] and "card1.shop 最早 2026-05-20" in out["summary"] and out["truncated"] == []


@pytest.mark.asyncio
async def test_who_first_by_clue_and_in_chat_with_limit_note(ff):
    out = json.loads(await first.who_first(query="card1.shop"))
    assert out["terms"] == ["card1.shop"] and "matches_query" not in out["groups"][0] and out["groups"][0]["copies"] == 4
    assert out["scanned"] == 4 and len(out["groups"]) == 1  # 正文里没有这个域名的帖（搜索按词拆开匹配混进来的）不要
    out = json.loads(await first.who_first(query="card1.shop", chat_id=777, limit=2))
    assert out["scanned"] == 2 and out["truncated"] == ["「card1.shop」在「交流群」只翻了最近 2 条，更早的可能没翻到"]
    with pytest.raises(Exception, match="None of the chats"):
        await first.who_first(query="card1.shop", chat_id=999)
    with pytest.raises(Exception, match="query is required"):
        await first.who_first(query=" ")


@pytest.mark.asyncio
async def test_query_can_be_just_the_opening_lines(ff):
    out = json.loads(await first.who_first(query="GPT Plus 代充 120元 卡网 card1.shop"))
    assert out["groups"][0]["matches_query"] and "没有一组" not in out["summary"]
    # 抄了广告再加一大段自己的话，重合度被拉低，但原文整段都在里面，还算同一组
    long_copy = T + "\n" + "另外本店还有 Netflix 会员 Spotify 会员 YouTube 会员 Office 全家桶 Adobe 全家桶 价格优惠 售后保障 欢迎咨询 长期合作" * 2
    assert first.similar(first.shingles(T), first.shingles(long_copy), 0.5) and first.jaccard(first.shingles(T), first.shingles(long_copy)) < 0.5
    assert not first.similar(first.shingles("今日有货 秒发"), first.shingles(long_copy), 0.5)  # 太短的片段不算包含


@pytest.mark.asyncio
async def test_check_seller_first_seen(fake):
    fake.users["seller_a"] = A
    fake.common = [GROUP]
    fake.by_user[(777, 1)] = [msg(1, "GPT Plus 代充 120元 秒发", A, date=D(3, 1))]
    fake.search_msgs = [msg(9, "我是 @seller_a 代充", A, date=D(1, 5))]
    out = json.loads(await trace.check_seller(handle="@seller_a", probe_sites=False))
    assert out["profile"]["first_seen"] == {"date": "2026-01-05", "chat": "交流群", "how": "被人提到"}
    fake.search_msgs = []
    out = json.loads(await trace.check_seller(handle="@seller_a", probe_sites=False))
    assert out["profile"]["first_seen"] == {"date": "2026-03-01", "chat": "交流群", "how": "自己的首帖"}


@pytest.mark.asyncio
async def test_who_first_no_hits(ff):
    ff.search_msgs = []
    out = json.loads(await first.who_first(query="没人发过的话术 独一无二"))
    assert out["scanned"] == 0 and out["summary"].startswith("没搜到")


@pytest.mark.asyncio
async def test_accusations_report_first_seen(fake):
    fake.search_msgs = [
        msg(10, "@seller_a 是骗子，付款后不发货", B, date=D(9, 1)),
        msg(9, "我是 @seller_a 代充 找我", A, date=D(1, 5)),  # 他自己最早的广告也算「在群里出现」
    ]
    acc, aliases, seen = await trace._accusations("seller_a", 1)
    assert len(acc) == 1 and seen == {"date": "2026-01-05", "chat": "交流群", "by_self": True, "complete": True}
    fake.search_msgs = []
    assert (await trace._accusations("seller_a", 1))[2] is None
