"""价格行情：品类配对、行情统计（中位数 / 红线 / 趋势）、报价对行情、工具一轮、卡片联动。全部假客户端。"""
import json
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

import pytest

import tg_mcp.app
from tg_mcp import prices, trace
from tests.test_trace import A, B, C, GROUP, GROUP2, fake, msg, user  # noqa: F401

NOW = datetime.now(timezone.utc)


def test_extract_pairs_left_first_and_converts():
    got = {it["raw"]: it["category"] for it in prices.extract("Claude 5x 278rmb Claude 20x 475rmb GPT Pro 5x 130rmb GPT Pro 20x 180rmb")}
    assert got == {"278rmb": "Claude 5x", "475rmb": "Claude 20x", "130rmb": "GPT Pro 5x", "180rmb": "GPT Pro 20x"}
    assert prices.extract("GPT Pro 20x 25刀")[0]["cny"] == 180.0
    assert prices.extract("ChatGPT Pro 1300元")[0]["category"] == "GPT Pro"  # 没带 5x/20x 的是单独一档
    assert prices.extract("GPT Plus 月卡\n三合一订阅 33R") == [] and prices.extract("Claude Pro\n99元 GPT Plus") == [{"category": "GPT Plus", "cny": 99.0, "raw": "99元"}]  # 隔着换行不配，配后面同一行的
    assert prices.extract("Plus 120元 一个月，Claude 20x 850")[0]["category"] == "GPT Plus" and prices.extract("9元，GPT Plus 现货") == []  # 光说 Plus 算 GPT Plus；隔着逗号不配后面的
    assert prices.extract("一共 200元 包邮") == [] and prices.extract("GPT Plus 现货充足质保三十天售后无忧欢迎联系客服详询另有其他商品 128元") == []
    assert {it["category"] for it in prices.extract("Cursor 299元 Perplexity 99元")} == {"Cursor", "Perplexity"}
    # 品类和价格中间夹着时长 / 席位：另算一类，别拿月价行情判它
    assert [it["category"] for it in prices.extract("GPT Plus 7天 33R GPT Plus 1个月 113R GPT Team 1个席位 200元 Claude Pro 年付 999元 GPT Plus 合租 20元")] == [
        "GPT Plus·短期", "GPT Plus", "GPT Team·席位", "Claude Pro·年付", "GPT Plus·合租"]


def test_board_median_floor_trend_and_small_samples():
    today = "2026-10-29"
    q = lambda c, v, d, s="@x": {"category": c, "cny": v, "raw": f"{v}元", "seller": s, "date": d}
    quotes = [q("GPT Plus", v, "2026-10-28") for v in (100, 110, 120, 120, 130, 900)] + [q("Grok", 50, "2026-10-28"), q("Grok", 60, "2026-10-28")]
    b = prices.board(quotes, today=today)
    assert b["GPT Plus"]["n"] == 6 and b["GPT Plus"]["median"] == 120 and b["GPT Plus"]["mad"] == 10 and b["GPT Plus"]["floor"] == 90
    assert b["GPT Plus"]["min"] == 100 and b["GPT Plus"]["max"] == 900 and b["GPT Plus"]["trend_7d_pct"] is None and b["GPT Plus"]["cheapest"][0]["cny"] == 100
    # 几乎全报同一个价（MAD≈0）：红线夹到中位数八成，不然 498 都算低到不可能
    assert prices.board([q("GPT Pro 5x", v, "2026-10-28") for v in (499, 499, 499, 499, 499, 519)], today=today)["GPT Pro 5x"]["floor"] == 399.2
    assert b["Grok"] == {"n": 2, "note": "样本不够，不算行情", "values": [50, 60]}
    # 近 7 天 vs 之前：各 3 条以上才算趋势
    quotes = [q("GPT Plus", v, "2026-10-01") for v in (100, 100, 100)] + [q("GPT Plus", v, "2026-10-28") for v in (120, 120, 120)]
    assert prices.board(quotes, today=today)["GPT Plus"]["trend_7d_pct"] == 20
    assert prices.outliers([q("GPT Plus", 9, "2026-10-28", "@cheap"), q("GPT Plus", 120, "2026-10-28")], b) == [
        {"category": "GPT Plus", "cny": 9, "raw": "9元", "seller": "@cheap", "date": "2026-10-28", "floor": 90, "median": 120}]


def test_compare_verdicts():
    stats = {"GPT Plus": {"median": 120, "floor": 90}}
    items = [{"category": "GPT Plus", "cny": 9, "raw": "9元"}, {"category": "GPT Plus", "cny": 100, "raw": "100元"}, {"category": "GPT Plus", "cny": 118, "raw": "118元"},
             {"category": "GPT Plus", "cny": 150, "raw": "150元"}, {"category": "Grok", "cny": 50, "raw": "50元"}, {"category": "GPT Plus", "cny": 9, "raw": "9 元"}]
    vs = prices.compare(items, stats)
    assert [v["verdict"] for v in vs] == ["低于红线 90：低到不可能", "比行情便宜 17%", "跟行情差不多", "比行情贵 25%", "行情样本不够"]
    assert vs[0]["below_floor"] and vs[0]["diff_pct"] == -92 and vs[4]["market_median"] is None and len(vs) == 5  # 同品类同价只记一次


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setattr(tg_mcp.app, "ALLOWED_ROOTS", [tmp_path])
    monkeypatch.setattr(tg_mcp.app, "HTTP_MODE", True)
    monkeypatch.setattr(prices, "client", fake_client_holder["c"])
    return tmp_path


fake_client_holder = {}


@pytest.fixture
def pf(fake, monkeypatch):
    fake_client_holder["c"] = fake

    async def resolve(ident):
        return {777: GROUP, "demo_group": GROUP, 778: GROUP2}[ident]

    monkeypatch.setattr(prices, "resolve_entity", resolve)
    monkeypatch.setattr(prices, "client", fake)
    sellers = [user(100 + i, f"shop_{i}") for i in range(6)]
    old = NOW - timedelta(days=40)
    fake.msgs["demo_group"] = [msg(i + 1, f"GPT Plus 代充 {v}元 卡网 card{i}.shop", sellers[i]) for i, v in enumerate((100, 110, 120, 120, 130, 900))] + [
        msg(20, "GPT Plus 代充 100元", sellers[0]), msg(21, "GPT Plus 代充 100元", sellers[0]),  # 同一家刷同一价只算一条
        msg(22, "GPT Plus 9元 秒发", user(200, "too_cheap")),
        msg(23, "GPT Plus 代充 50元", sellers[1], date=old),  # 30 天以外，不算
    ] + [msg(30 + i, "详情看卡网 私聊", user(300, "silent_seller")) for i in range(6)]
    fake.msgs["othergroup"] = [msg(40, "Claude 20x 475rmb", sellers[2]), msg(41, "Grok 60元", sellers[3])]
    return fake


@pytest.mark.asyncio
async def test_price_board_tool(pf, roots):
    out = json.loads(await prices.price_board(chat_id=[777, 778]))
    assert out["posts_read"] == 17 and out["priced_posts"] == 11 and out["quotes"] == 9  # 30 天以外的那条不算；同一家刷同一价只算一条报价
    gp = out["board"]["GPT Plus"]
    assert gp["n"] == 7 and gp["median"] == 120 and gp["floor"] == 90 and gp["sellers"] == 7 and gp["cheapest"][0] == {"seller": "@too_cheap", "cny": 9, "raw": "9元"}
    assert out["board"]["Claude 20x"]["note"] == "样本不够，不算行情"
    assert [o["seller"] for o in out["outliers"]] == ["@too_cheap"] and out["never_quote"] == [{"seller": "@silent_seller", "posts": 6, "priced": 0}]
    assert out["saved"] == str(roots / "downloads" / "prices.json") and json.loads((roots / "downloads" / "prices.json").read_text())["board"]["GPT Plus"]["median"] == 120
    assert "GPT Plus 中位 120 元（7 条，红线 90）" in out["summary"] and "最离谱：@too_cheap 的 GPT Plus 9元" in out["summary"] and "@silent_seller 6 条 0 带价" in out["summary"]
    with pytest.raises(Exception, match="Give chat_id"):
        await prices.price_board()


@pytest.mark.asyncio
async def test_check_seller_grades_quotes_against_board(pf, roots):
    await prices.price_board(chat_id=777)
    pf.users["too_cheap"] = user(200, "too_cheap")
    pf.common = [GROUP]
    pf.by_user[(777, 200)] = [msg(50, "GPT Plus 9元 秒发 今日有货", user(200, "too_cheap"))]
    pf.search_msgs = []
    out = json.loads(await trace.check_seller(handle="@too_cheap", probe_sites=False))
    assert out["prices_vs_market"] == [{"item": "GPT Plus", "raw": "9元", "cny": 9.0, "market_median": 120.0, "diff_pct": -92, "below_floor": True, "verdict": "低于红线 90：低到不可能"}]
    assert out["tier"] == "C" and any(f.startswith("报价低到不可能：GPT Plus 9元（行情中位 120 元）") for f in out["red_flags"])
    pf.by_user[(777, 200)] = [msg(51, "GPT Plus 118元 秒发", user(200, "too_cheap"))]
    out = json.loads(await trace.check_seller(handle="@too_cheap", probe_sites=False))
    assert out["tier"] == "A" and out["prices_vs_market"][0]["verdict"] == "跟行情差不多"
