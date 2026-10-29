"""群画像：广告判定、活跃号、机器人、疑似同一人配对、骂人统计、报告。全部假客户端。"""
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

import pytest

import tg_mcp.app
from tg_mcp import group_profile as gp
from tests.test_trace import A, B, C, GROUP, W1, fake, msg, user  # noqa: F401


def D(m, d):
    return datetime(2026, m, d, tzinfo=timezone.utc)


A2 = user(11, "seller_a2")
BOT = user(12, "spam_bot", bot=True)
E = user(13, "victim_01")
AD = f"GPT Plus 代充 120元 卡网 card1.shop 秒发 收款 {W1}"
SAME = "Claude Max 5x 会员 今日特价 稳定不掉 售后无忧 需要的私聊我拿货源渠道"
POSTS = [
    msg(1, "大家好 今天天气不错", E, date=D(10, 20)),  # 最早一条（假客户端按列表顺序当「第一条」）
    msg(2, AD, A, date=D(10, 21)), msg(3, AD, A, date=D(10, 22)), msg(4, "Plus 代充 98元 card1.shop", A, date=D(10, 23)),
    msg(5, "出 Plus 110元 https://card1.shop/buy", A2, date=D(10, 23)), msg(6, "出 Plus 110元 https://card1.shop/buy", A2, date=D(10, 24)),
    msg(7, SAME, B, date=D(10, 24)), msg(8, SAME, B, date=D(10, 25)),
    msg(9, SAME, C, date=D(10, 25)), msg(10, SAME + " 还有 Gemini", C, date=D(10, 26)),
    msg(11, "机器人广播 1", BOT, date=D(10, 26)), msg(12, "机器人广播 2", BOT, date=D(10, 27)), msg(13, "机器人广播 3", BOT, date=D(10, 27)),
    msg(14, "@seller_a 是骗子 跑路了 大家别买", E, date=D(10, 28)),
    msg(15, "@seller_a 骗子 我也被坑了", E, date=D(10, 29)),  # 「骗了」不在负面词表里，「骗子」在
]


@pytest.fixture
def g(fake, monkeypatch):
    async def resolve(ident):
        if ident in (777, "demo_group"):
            return GROUP
        raise ValueError(ident)

    async def participants(entity, **kw):
        return [BOT, user(14, "other_bot", bot=True)]

    monkeypatch.setattr(gp, "client", fake)
    monkeypatch.setattr(gp, "resolve_entity", resolve)
    fake.get_participants = participants
    fake.msgs["demo_group"] = list(POSTS)
    return fake


def test_is_ad():
    assert gp.is_ad("GPT Plus 代充 120元") and gp.is_ad("看这个 card1.shop") and gp.is_ad("代充找我 @seller_a")
    assert not gp.is_ad("大家好 今天天气不错") and not gp.is_ad("代充太贵了")  # 叫卖词但没联系方式不算


@pytest.mark.asyncio
async def test_group_profile_numbers(g):
    out = json.loads(await gp.group_profile(chat_id=777))
    assert out["chat"]["title"] == "交流群" and out["chat"]["members"] == 10 and out["chat"]["opacity"] == ["新成员看不到入群前历史"] and out["chat"]["first_post"] == "2026-10-20"
    s = out["sample"]
    assert s["posts"] == 15 and s["from"] == "2026-10-20" and s["to"] == "2026-10-29" and s["days"] == 9 and s["senders"] == 6
    assert s["bots_posting"] == 1 and s["bot_posts_share"] == 20 and s["bot_members"] == 2
    # 广告：A 3 条 + A2 2 条 + B/C 4 条「私聊我」(叫卖词但没 @号，不算) → 5 条带价/链接
    assert out["ads"]["posts"] == 5 and out["ads"]["ratio"] == 33 and out["ads"]["priced"] == 5 and out["ads"]["with_links"] == 5
    assert out["ads"]["top_domains"] == ["card1.shop"] and out["ads"]["wallets"] == [W1] and "120元" in out["ads"]["prices"]
    top = out["top_senders"]
    assert [(r["handle"], r["posts"]) for r in top[:3]] == [("@seller_a", 3), ("@victim_01", 3), ("@spam_bot", 3)] and top[0]["ads"] == 3 and top[0]["wallets"] == [W1]
    assert out["concentration_top3"] == 60
    pairs = {(p["a"], p["b"]): p["why"] for p in out["related_pairs"]}
    assert pairs[("@seller_a", "@seller_a2")] == "同一个卡网 card1.shop" and pairs[("@seller_b", "@seller_c")] == "发过一样的文案"
    assert out["negative"] == {"posts": 2, "ratio": 13, "most_accused": [{"handle": "@seller_a", "times": 2}]}
    assert out["verdict"] == "聊天和广告混着"
    assert "「交流群」聊天和广告混着。10 人，2026-10-20 建，近 15 条帖跨 9 天" in out["summary"] and "被点名最多 @seller_a（2 次）" in out["summary"] and "主要卡网：card1.shop" in out["summary"]


def test_verdicts():
    assert gp.verdict("channel", 80, 100, 0) == "单向发布的频道，内容基本是广告"
    assert gp.verdict("group", 70, 65, 0) == "卖场：广告为主，且被少数几个号刷屏"
    assert gp.verdict("group", 10, 30, 50) == "机器人刷屏的群"
    assert gp.verdict("group", 10, 30, 0) == "聊天讨论为主"


@pytest.mark.asyncio
async def test_group_profile_report_and_empty(g, tmp_path, monkeypatch):
    monkeypatch.setattr(tg_mcp.app, "ALLOWED_ROOTS", [tmp_path])
    monkeypatch.setattr(tg_mcp.app, "HTTP_MODE", True)
    out = json.loads(await gp.group_profile(chat_id="demo_group", report=True))
    page = tmp_path / "downloads" / out["report"].split("/")[-1]
    assert page.exists() and "群画像：交流群" in page.read_text() and "@seller_a" in page.read_text()
    g.msgs["demo_group"] = []
    out = json.loads(await gp.group_profile(chat_id=777))
    assert out["sample"]["posts"] == 0 and "读不到" in out["summary"]
    with pytest.raises(Exception):
        await gp.group_profile(chat_id=999)
