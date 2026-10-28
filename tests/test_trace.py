"""一键溯源 / 卖家一眼看。全部用假 client，不碰网络：抓站的 probe_site 也换成假的。"""
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

import httpx
import pytest
from telethon import errors, functions

from tg_mcp import trace
from tg_mcp.common import norm_url

NOW = datetime.now(timezone.utc)


def user(id, username, bot=False, **kw):
    return SimpleNamespace(id=id, username=username, first_name=username, last_name=None, bot=bot, **kw)


GROUP = SimpleNamespace(id=777, title="交流群", username="demo_group", broadcast=False, megagroup=True)
GROUP2 = SimpleNamespace(id=778, title="另一个群", username="othergroup", broadcast=False, megagroup=True)
A, B, C = user(1, "seller_a"), user(2, "seller_b"), user(3, "seller_c")
UPSTREAM = user(9, "upstream_bot", bot=True)
HELPER = user(8, "helper_01")


def msg(id, text, sender, date=None, **kw):
    base = dict(id=id, message=text, date=date or NOW - timedelta(days=1), sender=sender, sender_id=sender.id,
                peer_id=None, fwd_from=None, forward=None, edit_date=None, via_bot_id=None,
                chat=SimpleNamespace(title="交流群"))
    base.update(kw)
    return SimpleNamespace(**base)


class FakeClient:
    def __init__(self):
        self.msgs, self.by_user, self.search_msgs = {}, {}, []
        self.users, self.about, self.common = {}, "", []

    async def get_messages(self, entity, limit=None, search=None, from_user=None, **kw):
        if entity is None:
            return self.search_msgs[:limit]
        key = getattr(entity, "username", None) or getattr(entity, "id", None)
        if from_user is not None:
            return self.by_user.get((entity.id, from_user.id), [])[:limit]
        return self.msgs.get(key, [])[:limit]

    async def iter_messages(self, entity, limit=None, search=None, **kw):
        self.last_iter_kw = kw
        for m in (await self.get_messages(entity, limit=limit, search=search)):
            yield m

    async def __call__(self, request):
        if isinstance(request, functions.contacts.ResolveUsernameRequest):
            u = self.users.get(request.username)
            if u is None:
                raise errors.UsernameNotOccupiedError(request=None)
            return SimpleNamespace(users=[u], chats=[])
        if isinstance(request, functions.users.GetFullUserRequest):
            return SimpleNamespace(full_user=SimpleNamespace(about=self.about, common_chats_count=len(self.common)))
        if isinstance(request, functions.messages.GetCommonChatsRequest):
            return SimpleNamespace(chats=self.common)
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return SimpleNamespace(full_chat=SimpleNamespace(about="", participants_count=10, hidden_prehistory=True, linked_chat_id=None))
        raise AssertionError(type(request))


SITES = {
    "card1.shop": {"verdict": "reachable", "ip": "1.2.3.4", "title": "店1", "contacts": ["upstream_bot"],
                      "calls": ["api.upstream-demo.com"], "platform": "独角数卡"},
    "card2.shop": {"verdict": "reachable", "ip": "1.2.3.4", "calls": ["api.upstream-demo.com"]},
    "api.upstream-demo.com": {"verdict": "reachable", "ip": "5.6.7.8", "platform": "ZovoCard 卡台"},
}


@pytest.fixture
def fake(monkeypatch):
    fc = FakeClient()
    entities = {"demo_group": GROUP, "upstream_bot": UPSTREAM, "helper_01": HELPER, 777: GROUP, 778: GROUP2}

    async def resolve(ident):
        if ident in entities:
            return entities[ident]
        raise ValueError(f"no entity {ident}")

    async def probe(host):
        return {"host": host, **SITES.get(host, {"verdict": "probe_failed", "error": "dns"})}

    async def public(query, limit):
        return [], None

    monkeypatch.setattr(trace, "client", fc)
    monkeypatch.setattr(trace, "resolve_entity", resolve)
    monkeypatch.setattr(trace, "probe_site", probe)
    monkeypatch.setattr(trace, "_public_posts", public)
    monkeypatch.setattr(trace, "_PAUSE", 0)
    return fc


SEED = [
    msg(1, "GPT Plus 代充 120元 卡网 card1.shop", A),
    msg(2, "Claude 代充 https://card1.shop/buy 找 @helper_01", B),
    msg(3, "出 Plus https://card2.shop", C),
]


# ---------- 一键溯源 ----------


async def test_trace_converges_on_shared_upstream(fake):
    fake.msgs["demo_group"] = SEED
    out = json.loads(await trace.trace_source(chat_id="demo_group", depth=2))
    assert out["posts_read"] == 3 and len(out["sellers"]) == 3
    top = out["candidates"][0]
    # 两家店的代码都调同一个卡台，而且它更深一层 —— 它才是源头候选，不是被两个卖家贴的店
    assert top["node"] == "api.upstream-demo.com" and top["score"] == 2
    assert any("调用" in r for r in top["reasons"]) and "平台：ZovoCard 卡台" in top["reasons"]
    shop1 = next(c for c in out["candidates"] if c["node"] == "card1.shop")
    assert any("同一台服务器" in r and "card2.shop" in r for r in shop1["reasons"])
    assert out["handles"]["@upstream_bot"]["kind"] == "bot"
    assert "@helper_01" in out["handles"]
    assert out["sites"]["card1.shop"]["platform"] == "独角数卡"
    assert "独角数卡" in out["mermaid"] and "-->|调用|" in out["mermaid"] and "-->|联系方式|" in out["mermaid"]
    assert ":::source" in out["mermaid"] and out["truncated"] == []
    assert "汇聚点：api.upstream-demo.com" in out["summary"]


async def test_trace_budget_and_floodwait_are_reported_not_fatal(fake, monkeypatch):
    fake.msgs["demo_group"] = SEED

    async def flood(ident):
        if ident == "demo_group":
            return GROUP
        raise errors.FloodWaitError(request=None, capture=30)  # 只在核对 @号 时限速

    monkeypatch.setattr(trace, "resolve_entity", flood)
    out = json.loads(await trace.trace_source(chat_id="demo_group", depth=2, max_sites=1))
    notes = " ".join(out["truncated"])
    assert "1 个站没查" in notes and "限速 30 秒" in notes
    assert out["mermaid"].startswith("graph LR")
    assert len(out["sites"]) == 1  # 只查了被指最多的那一个


async def test_trace_query_path_mixes_my_chats_and_public_posts(fake, monkeypatch):
    fake.search_msgs = SEED[:1]

    async def public(query, limit):
        return [{"id": 5, "date": None, "edit_date": None, "chat": "公开频道", "text": "官方 https://card1.shop",
                 "sender": "@pubchan", "sender_label": "公开频道甲", "sender_kind": "channel",
                 "fwd": None, "fwd_label": None, "fwd_old": False, "edited_old": False, "via_bot": False}], None

    monkeypatch.setattr(trace, "_public_posts", public)
    out = json.loads(await trace.trace_source(query="GPT Plus", depth=1))
    assert {s["handle"] for s in out["sellers"]} == {"@seller_a", "@pubchan"}
    assert out["candidates"][0]["node"] == "card1.shop"


async def test_trace_caps_posts_per_sender(fake):
    fake.msgs["demo_group"] = [msg(i, f"第{i}条 https://card1.shop", A) for i in range(12)] + [msg(99, "https://card2.shop", B)]
    out = json.loads(await trace.trace_source(chat_id="demo_group", depth=1))
    assert {s["handle"]: s["posts"] for s in out["sellers"]} == {"@seller_a": 5, "@seller_b": 1}


def test_ip_class_tells_proxy_fake_ip_from_private():
    assert trace._ip_class(["198.18.1.175"]) == "fakeip"
    assert trace._ip_class(["198.18.1.175", "10.0.0.1"]) == "private"
    assert trace._ip_class(["8.8.8.8"]) == "public" and trace._ip_class([]) == "private"


async def test_trace_without_query_reads_only_link_posts(fake):
    from telethon import types

    fake.msgs["demo_group"] = SEED
    await trace.trace_source(chat_id="demo_group", depth=1)
    assert isinstance(fake.last_iter_kw["filter"], types.InputMessagesFilterUrl)
    await trace.trace_source(chat_id="demo_group", query="GPT", depth=1)
    assert "filter" not in fake.last_iter_kw


async def test_trace_accepts_several_chats(fake):
    fake.msgs["demo_group"] = SEED[:2]
    fake.msgs["othergroup"] = SEED[2:]
    out = json.loads(await trace.trace_source(chat_id=["demo_group", 778, "nowhere_group"], depth=1))
    assert out["posts_read"] == 3 and out["truncated"] == ["nowhere_group 打不开：ValueError"]


async def test_group_members_enter_graph_only_with_clues(fake):
    chatty, linker = user(21, "chatty_guy"), user(22, "linker_guy")
    fake.msgs["demo_group"] = [msg(1, "GPT 代充 找 @othergroup", A)]
    fake.msgs["othergroup"] = [msg(2, "今天天气不错", chatty), msg(3, "卡网 https://card9.shop", linker)]
    fake_entities = {"othergroup": GROUP2}

    async def resolve(ident):
        return fake_entities.get(ident) or GROUP

    import tg_mcp.trace as tr
    tr.resolve_entity = resolve
    out = json.loads(await trace.trace_source(chat_id="demo_group", depth=2))
    assert "@linker_guy" in out["mermaid"] and "@chatty_guy" not in out["mermaid"]
    assert "-->|在群里发帖|" in out["mermaid"]
    assert out["candidates"] == []  # 一个群只被一个卖家提到、一个成员贴链接：成员发帖不算汇聚分


async def test_trace_needs_query_or_chat(fake):
    with pytest.raises(Exception, match="query, chat_id and/or address"):
        await trace.trace_source()


async def test_trace_no_posts(fake):
    fake.msgs["demo_group"] = []
    assert json.loads(await trace.trace_source(chat_id="demo_group"))["posts_read"] == 0


# ---------- 线索抽取、抓站的保险 ----------


def test_hosts_and_handles_in_text():
    assert trace.hosts_in("卡网 abc.shop 另见 https://www.xyz.com/a 和 https://t.me/foo 以及 docs.google.com") == {"abc.shop", "xyz.com"}
    assert trace.hosts_in("截图 report.pdf 和 cdn.jsdelivr.net 和 https://static.foo.com/a.js 和 wa.me") == set()
    assert trace.handles_in("找 @Seller_01 或 t.me/upstream_bot，别点 t.me/joinchat/xxx，邮箱 a@b.com 或 @outlook.com") == {"seller_01", "upstream_bot"}


def test_site_clues_skips_css_and_cdn():
    html = ('<title> 某某 卡网 </title><style>@media (x){}</style><script src="https://cdn.jsdelivr.net/x.js"></script>'
            '<script>fetch("https://api.upstream-demo.com/v1")</script><a href="https://t.me/upstream_bot">客服</a> 客服 @helper_01 独角数卡')
    c = trace.site_clues("shop.example.com", html)
    assert c == {"title": "某某 卡网", "contacts": ["helper_01", "upstream_bot"], "calls": ["api.upstream-demo.com"], "platform": "独角数卡"}


def test_public_ip_guard():
    assert not trace._public_ip("127.0.0.1") and not trace._public_ip("10.0.0.1") and not trace._public_ip("x")
    assert trace._public_ip("8.8.8.8")


async def test_probe_refuses_private_hosts_without_any_http(monkeypatch):
    async def ips(host):
        return ["192.168.1.1"]

    class Boom:
        def __init__(self, *a, **kw):
            raise AssertionError("不该发请求")

    monkeypatch.setattr(trace, "_resolve_ips", ips)
    monkeypatch.setattr(httpx, "AsyncClient", Boom)
    rec = await trace.probe_site("router.local")
    assert rec["verdict"] == "probe_failed" and rec["error"] == "non_public_ip"


def test_norm_url_keeps_web_prefix():
    assert norm_url("https://web.example.com/x") == "web.example.com/x"
    assert norm_url("https://www.example.com/") == "example.com"


# ---------- 卖家一眼看 ----------


async def test_check_seller_gone_is_tier_d(fake):
    out = json.loads(await trace.check_seller(handle="@ghost_seller"))
    assert out["tier"] == "D" and "已不存在" in out["red_flags"][0]


async def test_check_seller_phishing_and_accusations(fake):
    fake.users["seller_a"] = A
    fake.common = [GROUP]
    fake.by_user[(777, 1)] = [
        msg(1, "GPT Plus 代充 120元 卡网 card1.shop 秒发 售后无忧", A),
        msg(2, "Claude Max 5x 278rmb 卡网 card1.shop 今日有货", A),
    ]
    fake.search_msgs = [
        msg(10, "@seller_a 是骗子，付款后不发货", B),
        msg(11, "被 @seller_a 骗了 跑路 大家注意", C),
        msg(12, "他从 @seller_a 改成了 @seller_a2", C),
        msg(13, "@seller_a 不错，昨天买的到了", B),  # 没有负面词，不算指认
    ]
    SITES["card1.shop"] = {**SITES["card1.shop"], "verdict": "cloudflare_phishing"}
    try:
        out = json.loads(await trace.check_seller(handle="@seller_a"))
    finally:
        SITES["card1.shop"]["verdict"] = "reachable"
    assert out["tier"] == "D"
    flags = " ".join(out["red_flags"])
    assert "钓鱼：card1.shop" in flags and "被指认 2 次" in flags and "改过名 / 有小号：@seller_a2" in flags
    assert {e["quote"] for e in out["evidence"]} == {"@seller_a 是骗子，付款后不发货", "被 @seller_a 骗了 跑路 大家注意"}
    assert {a["handle"]: a["why"][:4] for a in out["related_accounts"]} == {"@seller_a2": "群友说：", "@upstream_bot": "卡网 c"}
    assert out["activity"]["sampled_from"] == ["交流群"] and out["activity"]["posts_with_price"] == 2
    assert out["clues"]["prices"] == ["120元", "278rmb"]
    assert out["upstream_hints"] == ["card1.shop 用的是 独角数卡"]
    assert out["confidence"] == "high"


async def test_check_seller_spam_is_tier_c(fake):
    fake.users["seller_a"] = A
    fake.common = [GROUP]
    fake.by_user[(777, 1)] = [msg(i, "Claude Max 5x 278rmb 卡网 card3.shop 秒发 售后无忧", A) for i in range(10)]
    out = json.loads(await trace.check_seller(handle="@seller_a", probe_sites=False))
    assert out["tier"] == "C" and "刷屏：10 条只有 1 条不同文案（10.0 倍）" in " ".join(out["red_flags"])
    assert out["sites"] == {}


async def test_check_seller_stale_posts_is_tier_c(fake):
    fake.users["seller_a"] = A
    fake.common = [GROUP]
    old = NOW - timedelta(days=90)
    fake.by_user[(777, 1)] = [
        msg(1, "GPT Plus 代充 120元 一年质保 全程售后", A, date=old, edit_date=NOW - timedelta(days=1)),
        msg(2, "Claude Pro 代充 150元 稳定不掉 当天开通", A, fwd_from=SimpleNamespace(from_id=None, date=old, from_name="老频道")),
        msg(3, "Gemini 代充 60元 可查可验 欢迎咨询", A),
    ]
    out = json.loads(await trace.check_seller(handle="@seller_a", probe_sites=False))
    assert out["tier"] == "C" and "3 条里 2 条是老帖改价或转发老广告" in " ".join(out["red_flags"])
    assert out["provenance"] == {"edited_old": 1, "forwarded_old": 1, "via_bot": 0, "forwarded_from": ["老频道"]}
    assert len(out["evidence"]) == 2 and "改于" in out["evidence"][0]["quote"]


async def test_check_seller_clean_recent_priced_is_tier_a(fake):
    fake.users["seller_a"] = A
    fake.common = [GROUP]
    fake.by_user[(777, 1)] = [
        msg(1, "GPT Plus 代充 120元 一年质保 全程售后", A),
        msg(2, "Claude Pro 代充 150元 稳定不掉 当天开通", A),
    ]
    out = json.loads(await trace.check_seller(handle="@seller_a", probe_sites=False))
    assert out["tier"] == "A" and out["red_flags"] == [] and out["confidence"] == "medium"


async def test_check_seller_user_without_posts_has_low_confidence(fake):
    fake.users["seller_a"] = A
    out = json.loads(await trace.check_seller(handle="@seller_a", probe_sites=False))
    assert out["confidence"] == "low" and "source_chats" in out["red_flags"][-1]
    assert out["tier"] == "B"


async def test_check_seller_channel_reads_own_posts_and_opacity(fake):
    chan = SimpleNamespace(id=55, title="某卖家频道", username="sellerchan", broadcast=True, megagroup=False, noforwards=True)
    fake.users["sellerchan"] = chan
    fake.msgs["sellerchan"] = [msg(i, f"第{i}天 GPT Plus 代充 120元 一年质保 全程售后", chan) for i in range(3)]
    out = json.loads(await trace.check_seller(handle="@sellerchan", probe_sites=False))
    assert out["profile"]["kind"] == "channel" and out["profile"]["members"] == 10
    assert "结构不透明" in " ".join(out["red_flags"]) and out["tier"] == "C"


# ---------- 钱的去向 ----------

import hashlib


def tron_addr(seed: int) -> str:
    """造一个过校验和的波场地址（测试用）。"""
    body = b"\x41" + hashlib.sha256(str(seed).encode()).digest()[:20]
    raw = body + hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = trace._B58[r] + out
    return out


W1, W2, W3, W4 = tron_addr(1), tron_addr(2), tron_addr(3), tron_addr(4)
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def tx(frm, to, usdt, ts=1_790_000_000):
    return {"from": frm, "to": to, "value": str(int(usdt * 1_000_000)), "token_info": {"symbol": "USDT", "decimals": 6}, "block_timestamp": ts * 1000}


def test_tron_address_validation():
    assert trace.valid_tron(USDT_CONTRACT) and trace.valid_tron(W1)
    assert not trace.valid_tron(USDT_CONTRACT[:-1] + "A")  # 改一个字就不是地址
    assert trace.wallets_in(f"打 U 到 {W1} 或 0xAbCdef0123456789abcdef0123456789ABCDEF01 或 bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq") == {
        ("tron", W1), ("eth", "0xabcdef0123456789abcdef0123456789abcdef01"), ("btc", "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq")}
    assert trace.wallets_in("TXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX 不是地址") == set()


def test_summarize_transfers():
    approval = {**tx(W1, W4, 0), "type": "Approval", "value": str(2 ** 256 - 1)}  # 授权记录，不是钱动了
    s = trace.summarize_transfers(W1, [tx(W2, W1, 100), tx(W2, W1, 50, 1_790_100_000), tx(W1, W3, 120), tx(W1, W4, 20), approval])
    assert (s["tx"], s["in_usdt"], s["out_usdt"], s["payers"], s["payees"]) == (5, 150, 140, 1, 2)
    assert s["top_in"] == [{"address": W2, "usdt": 150, "count": 2}] and s["top_out"][0]["address"] == W3
    assert s["first"] == "2026-09-21" and s["last"] == "2026-09-22" and s["note"] is None and W1 in s["explorer"]


@pytest.fixture
def chain(monkeypatch):
    ledger = {W1: [tx(W2, W1, 1000), tx(W1, W3, 900), tx(W1, W4, 50)], W3: [tx(W1, W3, 900), tx(W3, W4, 880)]}

    async def transfers(addr, limit=200):
        if addr == "T" + "9" * 33:
            raise RuntimeError("trongrid_403")
        return ledger.get(addr, [])

    monkeypatch.setattr(trace, "_tron_transfers", transfers)
    return ledger


async def test_trace_shared_wallet_means_same_person(fake, chain):
    fake.msgs["demo_group"] = [msg(1, f"Claude 代充 收 U {W1}", A), msg(2, f"GPT 代充 打到 {W1}", B)]
    out = json.loads(await trace.trace_source(chat_id="demo_group", depth=2))
    top = out["candidates"][0]
    assert top["node"] == W1 and "2 个卖家用它收款（同一个人）" in top["reasons"]
    assert W1 not in {c["node"] for c in json.loads(await trace.trace_source(address=W1, depth=1))["candidates"]}  # 起点不算汇聚点
    assert any("链上进 1000U、出 950U" in r for r in top["reasons"])
    assert out["wallets"][W1]["top_out"][0]["address"] == W3 and out["wallets"][W3]["out_usdt"] == 880
    assert "-->|收款地址|" in out["mermaid"] and "-->|转出 900U/1笔|" in out["mermaid"] and ":::wallet" in out["mermaid"]
    assert "查了 3 个钱包" in out["summary"]  # W4 也查了，只是没流水


async def test_trace_from_address_follows_money(fake, chain):
    out = json.loads(await trace.trace_source(address=W1, depth=2, max_wallets=1))
    assert out["posts_read"] == 0 and set(out["wallets"]) == {W1}
    assert any("钱包没查" in n for n in out["truncated"])
    with pytest.raises(Exception, match="valid Tron"):
        await trace.trace_source(address="T" + "x" * 33)


async def test_trace_wallet_api_refusal_is_reported(fake, chain):
    bad = "T" + "9" * 33
    monkeypatch_valid = trace.valid_tron
    trace.valid_tron = lambda a: a == bad or monkeypatch_valid(a)
    try:
        out = json.loads(await trace.trace_source(address=bad, depth=1))
    finally:
        trace.valid_tron = monkeypatch_valid
    assert out["wallets"][bad]["error"] == "trongrid_403" and any("限速" in n for n in out["truncated"])


async def test_check_seller_money_section(fake, chain):
    fake.users["seller_a"] = A
    fake.common = [GROUP]
    fake.by_user[(777, 1)] = [msg(1, f"GPT Plus 120元 收款 {W1} 秒发 售后无忧", A)]
    out = json.loads(await trace.check_seller(handle="@seller_a", probe_sites=False))
    assert out["money"][W1]["in_usdt"] == 1000 and out["money"][W1]["top_out"][0]["address"] == W3
    assert out["clues"]["signals"]["crypto"] == [W1]
    assert any("的钱主要转去" in h for h in out["upstream_hints"])
