"""市场雷达：快照压缩、比对规则、跑一轮、推送、定时判断。check_seller / trace_source 都换成假的，不碰网络。"""
import json
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

import pytest

import tg_mcp.app
from tg_mcp import radar

CARD = {
    "handle": "@seller_a", "tier": "B", "red_flags": [], "profile": {"id": 1, "username": "seller_a", "name": "胜山", "flags": []},
    "activity": {"last_post": "2026-10-28", "days_since_last": 1}, "clues": {"domains": ["card1.shop"], "prices": ["98 元"], "signals": {"crypto": ["TAAA"]}},
    "accusations": [], "money": {"TAAA": {"top_out": [{"address": "TBBB", "usdt": 100}]}}, "sites": {"card1.shop": {"verdict": "reachable"}},
}
GONE = {"handle": "@seller_a", "tier": "D", "red_flags": ["用户名已不存在（跑路、改名或被封）"], "evidence": []}
TRACE = {
    "posts_read": 30, "sellers": [{"handle": "user:1"}, {"handle": "user:2"}], "candidates": [{"node": "democms.com"}],
    "sites": {"democms.com": {}, "card1.shop": {}}, "wallets": {}, "handles": {"democms_bot": {}},
}


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setattr(tg_mcp.app, "ALLOWED_ROOTS", [tmp_path])
    monkeypatch.setattr(tg_mcp.app, "HTTP_MODE", True)
    monkeypatch.setattr(radar, "_PAUSE", 0)
    monkeypatch.delenv("RADAR_NOTIFY_CMD", raising=False)
    return tmp_path


@pytest.fixture
def fakes(monkeypatch):
    """两个假工具：返回 state 里当前的卡片 / 溯源结果，测试中途可以换。"""
    state = {"card": CARD, "trace": TRACE, "raise": None, "calls": []}

    async def fake_check(handle, source_chats=None, ctx=None):
        state["calls"].append(("seller", handle))
        if state["raise"]:
            raise state["raise"]
        return json.dumps(state["card"])

    async def fake_trace(ctx=None, **kw):
        state["calls"].append(("trace", kw))
        return json.dumps(state["trace"])

    monkeypatch.setattr(radar, "check_seller", fake_check)
    monkeypatch.setattr(radar, "trace_source", fake_trace)
    return state


def test_snapshot_is_narrow_and_diff_rules():
    o = radar.snap_seller(CARD)
    assert o["alive"] and o["tier"] == "B" and o["domains"] == ["card1.shop"] and o["top_out"] == {"TAAA": "TBBB"} and o["sites"] == {"card1.shop": "reachable"}
    n = radar.snap_seller({
        **CARD, "tier": "D", "red_flags": ["群里被指认 3 次"], "profile": {**CARD["profile"], "name": "新名", "flags": ["scam"]},
        "activity": {"last_post": "2026-08-01", "days_since_last": 89}, "clues": {"domains": ["card2.shop"], "prices": ["98 元", "120 元"], "signals": {}},
        "accusations": [{}, {}, {}], "money": {"TAAA": {"top_out": [{"address": "TCCC", "usdt": 1}]}}, "sites": {"card1.shop": {"verdict": "cloudflare_phishing"}},
    })
    ch = radar.diff_seller(o, n)
    assert "B 档 → D 档" in ch and "被官方标记 scam" in ch and "改名：'胜山' → '新名'" in ch
    assert any(c.startswith("新红旗：群里被指认") for c in ch) and "新增 3 条指认（共 3）" in ch
    assert "卡网多了 1 个：card2.shop" in ch and "卡网少了 1 个：card1.shop" in ch and "报价多了 1 个：120 元" in ch
    assert "沉寂超过 60 天" in ch and f"收款地址 {radar._short('TAAA')} 的钱改转去 {radar._short('TCCC')}" in ch and "卡网 card1.shop：reachable → cloudflare_phishing" in ch
    assert radar.diff_seller(o, radar.snap_seller(GONE)) == ["跑路了：用户名已不存在"]
    assert radar.diff_seller(o, o) == []
    assert radar.diff_seller(o, {**o, "sites": {"card1.shop": "http_503"}}) == []  # 一次 503 是抖动，不报
    # 同一用户名 ID 变了 = 壳被转手
    assert "用户名换了主人（ID 1 → 2）" in radar.diff_seller(o, {**o, "id": 2})


def test_trace_diff_rules():
    o = radar.snap_trace(TRACE)
    assert o["sellers"] == ["user:1", "user:2"] and o["candidates"] == ["democms.com"]
    n = radar.snap_trace({**TRACE, "sellers": [{"handle": "user:1"}, {"handle": "user:3"}, {"handle": "user:4"}], "candidates": [{"node": "newcms.com"}], "sites": {**TRACE["sites"], "newcms.com": {}}})
    ch = radar.diff_trace(o, n)
    assert "卖家多了 2 个：user:3 user:4" in ch and "卖家少了 1 个：user:2" in ch and "汇聚点换了：democms.com → newcms.com" in ch and "站多了 1 个：newcms.com" in ch
    assert radar.diff_trace(o, {**o, "candidates": []}) == ["汇聚点没了（原来是 democms.com）"]
    assert radar.diff_trace(o, o) == []


@pytest.mark.asyncio
async def test_add_run_list_history_remove(roots, fakes):
    r = json.loads(await radar.market_radar(action="add", handle="@seller_a"))
    assert r["status"] == "added" and r["name"] == "@seller_a" and r["baseline"]["tier"] == "B" and (roots / "downloads" / "radar.json").exists()
    r = json.loads(await radar.market_radar(action="add", chat_id=["demo_group", 777], name="卡网线"))
    assert r["kind"] == "trace" and r["baseline"]["candidates"] == ["democms.com"]
    assert fakes["calls"][-1][1]["chat_id"] == ["demo_group", 777] and fakes["calls"][-1][1]["depth"] == 2
    # 没变化
    r = json.loads(await radar.market_radar(action="run"))
    assert r["changed"] == [] and sorted(r["unchanged"]) == ["@seller_a", "卡网线"] and r["notified"] is False and "没变化" in r["text"]
    # 卖家跑路 + 溯源出新卖家
    fakes["card"] = GONE
    fakes["trace"] = {**TRACE, "sellers": TRACE["sellers"] + [{"handle": "user:9"}]}
    r = json.loads(await radar.market_radar(action="run"))
    assert {c["name"]: c["changes"] for c in r["changed"]} == {"@seller_a": ["跑路了：用户名已不存在"], "卡网线": ["卖家多了 1 个：user:9"]}
    assert r["text"].startswith("📡 市场雷达") and "• @seller_a：跑路了：用户名已不存在" in r["text"] and "2 个有变化" in r["text"]
    assert r["notify_note"].startswith("没配 RADAR_NOTIFY_CMD")
    lst = json.loads(await radar.market_radar(action="list"))
    assert lst["watching"] == 2 and {w["name"]: w["snapshots"] for w in lst["watches"]} == {"@seller_a": 3, "卡网线": 3}
    assert {w["name"]: w["last"] for w in lst["watches"]}["@seller_a"] == "已消失" and "汇聚点 democms.com" in {w["name"]: w["last"] for w in lst["watches"]}["卡网线"]
    h = json.loads(await radar.market_radar(action="history", name="@seller_a"))
    assert [s["alive"] for s in h["history"]] == [True, True, False]
    r = json.loads(await radar.market_radar(action="run", name="卡网线"))
    assert r["checked"] == 1 and r["unchanged"] == ["卡网线"]
    r = json.loads(await radar.market_radar(action="remove", name="@seller_a"))
    assert r["watching"] == 1
    with pytest.raises(Exception, match="No watch named"):
        await radar.market_radar(action="history", name="@seller_a")
    with pytest.raises(Exception, match="Already watching"):
        await radar.market_radar(action="add", chat_id=777, name="卡网线")
    with pytest.raises(Exception, match="Give handle"):
        await radar.market_radar(action="add")


@pytest.mark.asyncio
async def test_floodwait_stops_run_and_keeps_baseline(roots, fakes):
    await radar.market_radar(action="add", handle="@seller_a")
    await radar.market_radar(action="add", handle="@seller_b")
    fakes["raise"] = RuntimeError("Rate limited by Telegram. Please wait 1200 seconds before retrying.")
    r = json.loads(await radar.market_radar(action="run"))
    assert len(r["errors"]) == 1 and r["skipped"] == ["@seller_b"] and r["checked"] == 1 and "查不了 2 个" in r["text"]
    h = json.loads(await radar.market_radar(action="history", name="@seller_a"))
    assert len(h["history"]) == 1  # 失败那次不进历史，下次还跟成功的那份比
    fakes["raise"] = None
    fakes["card"] = {**CARD, "tier": "C"}
    r = json.loads(await radar.market_radar(action="run"))
    assert [c["changes"] for c in r["changed"]] == [["B 档 → C 档"], ["B 档 → C 档"]]


@pytest.mark.asyncio
async def test_notify_command_gets_text_on_stdin(roots, fakes, monkeypatch):
    out = roots / "sent.txt"
    monkeypatch.setenv("RADAR_NOTIFY_CMD", f"cat > '{out}'")
    await radar.market_radar(action="add", handle="@seller_a")
    r = json.loads(await radar.market_radar(action="run"))
    assert r["notified"] is False and not out.exists()  # 没变化不吵人
    fakes["card"] = GONE
    r = json.loads(await radar.market_radar(action="run"))
    assert r["notified"] is True and r["notify_note"] == "已推送" and out.read_text() == r["text"]
    r = json.loads(await radar.market_radar(action="run", notify=False))
    assert r["notified"] is False and r["notify_note"] == "notify=False"
    monkeypatch.setenv("RADAR_NOTIFY_CMD", "exit 3")
    fakes["card"] = CARD
    r = json.loads(await radar.market_radar(action="run"))
    assert r["notified"] is False and "退出码 3" in r["notify_note"]


def test_due_uses_last_run_in_file():
    now = datetime.now(timezone.utc).astimezone()
    assert radar.due({"watches": {}, "runs": []}, 24) is False  # 没有对象不跑
    assert radar.due({"watches": {"a": {}}, "runs": []}, 24) is True
    recent = {"watches": {"a": {}}, "runs": [{"at": (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")}]}
    assert radar.due(recent, 24, now) is False and radar.due(recent, 2, now) is True
