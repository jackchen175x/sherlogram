"""马甲识别：信号抽取、并查集聚类（一强或两弱）、公共平台不算、曝光帖共现、改名句式、按号聚焦。全部假客户端。"""
import json
import os

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

import pytest

from tg_mcp import same_person as sp
from tests.test_trace import A, B, C, GROUP, GROUP2, W1, fake, msg, user  # noqa: F401

A2 = user(11, "seller_a2")
D = user(12, "shop_d")
E = user(13, "victim_e")
LONG = "Claude Max 5x 会员 今日特价 稳定不掉 售后无忧 需要的私聊我拿货源渠道 卡网 card2.shop 另有 Gemini Pro 现货"
POSTS = [
    msg(1, f"GPT Plus 代充 120元 收款 {W1}", A), msg(2, f"出 Claude Pro 98元 USDT 地址 {W1}", A2),  # 共用收款地址：一条强信号成案
    msg(3, LONG, B), msg(4, LONG, C),  # 一样的文案 + 同一个卡网：两条弱信号成案
    msg(5, "低价 Plus 看 card2.shop", D),  # 只跟 B/C 共一个卡网：差一条证据
    msg(6, "他把用户名从 @old_face 改成了 @new_face 大家注意", E),  # 群友明说改名：直接连边
    msg(7, f"曝光 @scam_x @scam_y 是一伙的 收款都是 {W1}", E),  # 曝光帖：两个号共现 + 地址算到他们头上 → 跟 A/A2 同组
]


@pytest.fixture
def s(fake, monkeypatch):
    async def resolve(ident):
        return {777: GROUP, "demo_group": GROUP, 778: GROUP2}[ident]

    monkeypatch.setattr(sp, "client", fake)
    monkeypatch.setattr(sp, "resolve_entity", resolve)
    fake.msgs["demo_group"] = list(POSTS)
    return fake


def test_signals_and_cluster_rules():
    posts = [sp._post(m, "交流群") for m in POSTS]
    sig, who = sp.signals(posts)
    assert W1 in sig["@seller_a"]["crypto"] and W1 in sig["@scam_x"]["crypto"] and sig["@old_face"]["rename"] == sig["@new_face"]["rename"]
    assert who["@scam_x"]["kind"] == "mentioned" and who["@seller_a"]["posts"] == 1
    assert not sig["@victim_e"]["crypto"]  # 曝光帖里的地址不算到发帖的受害者头上
    groups, kept, near = sp.cluster(sig)
    sets = sorted(sorted(g) for g in groups.values())
    assert sets == [["@new_face", "@old_face"], ["@scam_x", "@scam_y", "@seller_a", "@seller_a2"], ["@seller_b", "@seller_c"]]
    assert set(kept[("@seller_a", "@seller_a2")]) == {"crypto", "namekin"} and set(kept[("@seller_b", "@seller_c")]) == {"textfp", "url"}
    assert list(near) == [("@seller_b", "@shop_d"), ("@seller_c", "@shop_d")] and set(near[("@seller_b", "@shop_d")]) == {"url"}


def test_public_platform_is_not_a_person():
    posts = [sp._post(msg(i, "看 bighub.com 自助下单", user(50 + i, f"u{i}")), "群") for i in range(9)]
    sig, _ = sp.signals(posts)
    groups, kept, near = sp.cluster(sig)
    assert not groups and not near  # 9 个人共用的链接是公共平台，不连边


@pytest.mark.asyncio
async def test_same_person_by_groups(s):
    out = json.loads(await sp.same_person(chat_id=[777]))
    assert out["posts_read"] == 7 and out["senders"] == 10  # 6 个发帖人 + 被改名句式 / 曝光帖点名的 4 个号
    big = out["clusters"][0]
    assert big["size"] == 4 and big["strength"] == "强" and big["signals"] == ["共用收款地址", "同一条曝光帖点名", "名字同源"]
    assert [m["handle"] for m in big["members"]][:2] == ["@seller_a", "@seller_a2"] and big["evidence"][0]["why"] == "共用收款地址"
    assert {c["strength"] for c in out["clusters"]} == {"强", "中"} and len(out["clusters"]) == 3
    assert out["near_miss"][0]["pair"] == ["@seller_b", "@shop_d"] and list(out["near_miss"][0]["signals"]) == ["同一个卡网 / 链接"]
    assert out["summary"].startswith("读了 7 条帖、10 个号，聚出 3 组同一人（强证据 2 组）。 最大一组 4 个号：@seller_a @seller_a2") and "差一条证据的 2 对" in out["summary"]
    with pytest.raises(Exception, match="Give chat_id"):
        await sp.same_person()


@pytest.mark.asyncio
async def test_same_person_by_handle(s):
    s.users["seller_a"] = A
    s.common = [GROUP]
    s.by_user[(777, 1)] = [msg(20, f"老号广告 收款 {W1}", A)]
    out = json.loads(await sp.same_person(handle="@seller_a"))  # 没给群：用共同群
    assert out["handle"] == "@seller_a" and out["cluster"]["size"] == 4 and [m["handle"] for m in out["cluster"]["members"]][0] == "@seller_a"
    assert out["summary"].startswith("@seller_a：读了 8 条帖") and "疑似还有 3 个号是同一个人（强证据" in out["summary"]
    s.users["shop_d"] = D
    out = json.loads(await sp.same_person(handle="@shop_d", chat_id=777))
    assert out["cluster"] is None and "没找到" in out["summary"] and [r["pair"] for r in out["near_miss"]] == [["@seller_b", "@shop_d"], ["@seller_c", "@shop_d"]]
    out = json.loads(await sp.same_person(handle="@nobody", chat_id=777))
    assert out["cluster"] is None and "用户名已不存在" in out["summary"]
