"""AI 套话：开场三道闸、发第一句、读答复追下一问、抽线索、报告、停；卡片联动。全部假客户端，不发一条真消息。"""
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

import pytest

import tg_mcp.app
from tg_mcp import probe, trace
from tests.test_trace import A, B, GROUP, W1, fake, msg, user  # noqa: F401

CHANNEL = SimpleNamespace(id=900, title="批发频道", username="wholesale_ch", broadcast=True, megagroup=False)


@pytest.fixture
def pr(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(tg_mcp.app, "ALLOWED_ROOTS", [tmp_path])
    monkeypatch.setattr(tg_mcp.app, "HTTP_MODE", True)
    monkeypatch.setattr(probe, "client", fake)
    monkeypatch.setattr(probe, "_PAUSE_RANGE", (0, 0))
    fake.users["seller_a"] = A
    fake.users["seller_b"] = B
    fake.users["wholesale_ch"] = CHANNEL
    fake.sent = []
    fake.next_id = 100

    async def send_message(entity, text, **kw):
        fake.next_id += 1
        fake.sent.append((getattr(entity, "username", None), text))
        m = msg(fake.next_id, text, user(1, "me"), date=datetime.now(timezone.utc), out=True)
        fake.msgs.setdefault(entity.username, []).insert(0, m)
        return m

    async def get_entity(uid):
        return {A.id: A, B.id: B}[uid]

    fake.send_message = send_message
    fake.get_entity = get_entity
    return fake


def reply(pr, who, text, id):
    pr.msgs.setdefault(who.username, []).insert(0, msg(id, text, who, date=datetime.now(timezone.utc), out=False))


def test_clues_and_summary():
    c = probe.clues_from(["Plus 120元 一个月，Claude 20x 850", "走卡网 card1.shop 也可以直接转 U 到 " + W1, "我是拿货的，上家 @big_boss 很稳"])
    assert c["quotes"] == [{"item": "GPT Plus", "cny": 120.0, "raw": "120元"}]
    assert c["domains"] == ["card1.shop"] and c["wallets"] == [W1] and c["handles"] == ["@big_boss"] and c["payment"] == ["卡网", "USDT"] and c["role"] == "自称代理 / 拿货的"
    assert probe.clues_from(["我们自己做的号，一手货源"])["role"] == "自称一手 / 源头"
    p = {"handle": "@seller_a", "status": "done", "transcript": [{"from": "me", "text": "?"}, {"from": "them", "text": "!"}], "clues": c}
    s = probe.summary_of(p)
    assert s.startswith("@seller_a：问了 1 句、答了 1 句，问完了；报价 GPT Plus 120元") and "付款走 卡网 / USDT" in s and "自称代理 / 拿货的" in s and "提到 @big_boss" in s and "给了 1 个收款地址" in s


@pytest.mark.asyncio
async def test_start_check_report_flow(pr, tmp_path):
    out = json.loads(await probe.probe_seller(action="start", handle="@seller_a", product="Claude Pro"))
    assert out["status"] == "started" and out["asked"] == "你好，在吗？Claude Pro 现在什么价？" and out["remaining"] == 3
    assert pr.sent == [("seller_a", "你好，在吗？Claude Pro 现在什么价？")] and (tmp_path / "downloads" / "probes.json").exists()
    # 还没回：不追问
    out = json.loads(await probe.probe_seller(action="check"))
    assert out["probes"][0]["status"] == "waiting" and out["probes"][0]["new_replies"] == [] and len(pr.sent) == 1
    # 回了：记下来、抽线索、追第二问
    reply(pr, A, "在的 Claude Pro 98元 走卡网 card1.shop", 200)
    out = json.loads(await probe.probe_seller(action="check"))
    r = out["probes"][0]
    assert r["new_replies"] == ["在的 Claude Pro 98元 走卡网 card1.shop"] and r["asked_next"] == "怎么付款？走卡网还是直接转 U？" and r["clues"]["quotes"][0]["item"] == "Claude Pro"
    assert pr.sent[-1] == ("seller_a", "怎么付款？走卡网还是直接转 U？") and "1 个有新答复：@seller_a：问了 2 句、答了 1 句，等回复；报价 Claude Pro 98元" in out["summary"]
    # 两条回复一起到：都记下，只追一问
    reply(pr, A, "卡网付 也收 U", 201)
    reply(pr, A, "量大 90 一个 10 个起", 202)
    out = json.loads(await probe.probe_seller(action="check", handle="@seller_a"))
    assert len(out["probes"][0]["new_replies"]) == 2 and out["probes"][0]["asked_next"].startswith("量大有优惠吗")
    reply(pr, A, "我是代理 上家 @big_boss", 203)
    out = json.loads(await probe.probe_seller(action="check"))
    assert out["probes"][0]["asked_next"].startswith("你这是一手的还是代理的")
    reply(pr, A, "代理的 上家稳", 204)
    out = json.loads(await probe.probe_seller(action="check"))
    assert out["probes"][0]["status"] == "done" and out["probes"][0]["asked_next"] is None
    rep = json.loads(await probe.probe_seller(action="report", handle="SELLER_A"))  # 大小写、带不带 @ 都认
    assert json.loads(await probe.probe_seller(action="report", handle=1))["handle"] == "@seller_a"  # 数字 ID 也认
    assert rep["status"] == "done" and [t["from"] for t in rep["transcript"]] == ["me", "them", "me", "them", "them", "me", "them", "me", "them"]
    assert rep["clues"]["role"] == "自称代理 / 拿货的" and rep["clues"]["handles"] == ["@big_boss"] and rep["clues"]["payment"] == ["卡网", "USDT"]
    assert rep["summary"].startswith("@seller_a：问了 4 句、答了 5 句，问完了；报价 Claude Pro 98元")
    lst = json.loads(await probe.probe_seller(action="list"))
    assert lst["today_started"] == 1 and lst["probes"][0]["step"] == "4/4"
    # 卡片联动：check_seller 带出套话结果
    pr.common = [GROUP]
    pr.search_msgs = []
    card = json.loads(await trace.check_seller(handle="@seller_a", probe_sites=False))
    assert card["probe"]["status"] == "done" and card["probe"]["summary"].startswith("@seller_a：问了 4 句")


@pytest.mark.asyncio
async def test_guards_and_force(pr, monkeypatch):
    await probe.probe_seller(action="start", handle="@seller_a")
    with pytest.raises(Exception, match="7 天内已经问过"):
        await probe.probe_seller(action="start", handle="@seller_a")
    with pytest.raises(Exception, match="不到 10 分钟"):
        await probe.probe_seller(action="start", handle="@seller_b")
    monkeypatch.setattr(probe, "DAILY_CAP", 1)
    monkeypatch.setattr(probe, "MIN_GAP_MIN", 0)
    with pytest.raises(Exception, match="今天已经给 1 个陌生人开过场"):
        await probe.probe_seller(action="start", handle="@seller_b")
    out = json.loads(await probe.probe_seller(action="start", handle="@seller_b", force=True, questions=["老板在吗", "多少钱"]))
    assert out["asked"] == "老板在吗" and out["remaining"] == 1
    with pytest.raises(Exception, match="频道 / 群不能私聊"):
        await probe.probe_seller(action="start", handle="@wholesale_ch", force=True)
    with pytest.raises(Exception, match="用户名已不存在"):
        await probe.probe_seller(action="start", handle="@ghost", force=True)
    out = json.loads(await probe.probe_seller(action="stop", handle="@seller_b"))
    assert out["status"] == "stopped" and "已停" in out["summary"]
    with pytest.raises(Exception, match="没有 @nobody 的套话记录"):
        await probe.probe_seller(action="report", handle="@nobody")


@pytest.mark.asyncio
async def test_no_reply_gives_up(pr, tmp_path):
    await probe.probe_seller(action="start", handle="@seller_a")
    path = tmp_path / "downloads" / "probes.json"
    store = json.loads(path.read_text())
    store["probes"]["@seller_a"]["started_iso"] = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    path.write_text(json.dumps(store))
    out = json.loads(await probe.probe_seller(action="check"))
    assert out["probes"][0]["status"] == "no_reply" and "没回" in json.loads(await probe.probe_seller(action="report", handle="@seller_a"))["summary"]
