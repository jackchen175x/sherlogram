"""骗子库：名单文件的读写、合并、查询、导出，以及工具的五个动作。不连网络。"""
import json
import os
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

import pytest

import tg_mcp.app
from tg_mcp import scamlist, trace
from tests.test_trace import A, B, C, GROUP, SITES, W1, chain, fake, msg  # noqa: F401

CARD_D = {
    "handle": "@seller_a", "tier": "D", "red_flags": ["群里被指认 2 次：骗子、跑路"],
    "evidence": [{"dimension": "负面", "chat": "交流群", "date": "2026-10-01", "message_id": 10, "quote": "@seller_a 是骗子"}],
    "profile": {"id": 1, "kind": "user", "name": "胜山"}, "clues": {"domains": ["card1.shop"], "signals": {"crypto": [W1]}},
    "related_accounts": [{"handle": "@seller_a2", "why": "群友说"}],
}
CARD_CLEAN = {"handle": "@seller_b", "tier": "A", "red_flags": [], "evidence": [], "profile": {"id": 2, "kind": "user", "name": "奚云"}, "clues": {"domains": [], "signals": {}}}


def test_record_requires_id_and_evidence():
    r = scamlist.record_from_card(CARD_D, "@jack")
    assert r["user_id"] == 1 and r["usernames"] == ["seller_a"] and r["wallets"] == [W1] and r["related"] == ["@seller_a2"] and r["reporters"] == ["@jack"]
    with pytest.raises(ValueError, match="没有证据不入库"):
        scamlist.record_from_card(CARD_CLEAN, "@jack")
    r2 = scamlist.record_from_card(CARD_CLEAN, "@jack", note="付款后不发货，订单截图在手")
    assert r2["evidence"][0]["dimension"] == "人工" and r2["evidence"][0]["reporter"] == "@jack"
    with pytest.raises(ValueError, match="没有数字 ID"):
        scamlist.record_from_card({"handle": "@ghost", "tier": "D", "evidence": [{}]}, "@jack")


def test_merge_dedupes_and_keeps_worst_tier(tmp_path):
    a = scamlist.record_from_card(CARD_D, "@jack")
    b = {**scamlist.record_from_card({**CARD_D, "tier": "C", "handle": "@seller_a_new"}, "@ak"), "first_reported": "2026-09-01",
         "evidence": [a["evidence"][0], {"dimension": "人工", "date": "2026-09-01", "quote": "我也被骗了", "reporter": "@ak"}]}
    lib = scamlist.merge_records([a, b, {"user_id": 9, "evidence": []}, {"user_id": "x", "evidence": [{}]}])
    assert set(lib) == {1}
    m = lib[1]
    assert m["tier"] == "D" and m["usernames"] == ["seller_a", "seller_a_new"] and m["reporters"] == ["@jack", "@ak"]
    assert len(m["evidence"]) == 2 and m["first_reported"] == "2026-09-01"
    p = tmp_path / "scamlist.jsonl"
    scamlist.save(p, lib)
    assert scamlist.load(p) == lib
    assert scamlist.find(lib, "@Seller_A_NEW") == [m] and scamlist.find(lib, "1") == [m] and scamlist.find(lib, W1) == [m] and scamlist.find(lib, "CARD1.shop") == [m]
    assert scamlist.find(lib, "@nobody") == []


def test_export_formats():
    lib = scamlist.merge_records([scamlist.record_from_card(CARD_D, "@jack"), scamlist.record_from_card({**CARD_D, "tier": "C", "profile": {"id": 3, "name": "x"}}, "@jack")])
    csv_text = scamlist.export(lib, "rose_csv")
    assert csv_text.splitlines()[0] == "id,reason,firstname,lastname,username" and csv_text.count("\n") == 2  # 默认只导 D 档
    assert csv_text.splitlines()[1].startswith("1,D档：群里被指认 2 次：骗子、跑路（1 条证据，Sherlogram ")
    rj = scamlist.export(lib, "rose_json", min_tier="C")
    assert rj.count("\n") == 2 and json.loads(rj.splitlines()[0])["user_name"] == "seller_a"
    assert json.loads(scamlist.export(lib, "json").splitlines()[0])["evidence"]


@pytest.fixture
def roots(monkeypatch, tmp_path):
    monkeypatch.setattr(tg_mcp.app, "ALLOWED_ROOTS", [tmp_path])
    monkeypatch.setattr(tg_mcp.app, "HTTP_MODE", True)
    return tmp_path


def _seller_a_accused(fake):
    fake.users["seller_a"] = A
    fake.common = [GROUP]
    fake.by_user[(777, 1)] = [msg(1, f"GPT Plus 120元 收款 {W1} 卡网 card1.shop", A)]
    fake.search_msgs = [msg(10, "@seller_a 是骗子，付款后不发货", B), msg(11, "被 @seller_a 骗了 跑路", C)]

    async def get_me():
        return SimpleNamespace(id=100, username="jackchen")

    fake.get_me = get_me


async def test_tool_add_query_export_stats(fake, chain, roots):
    _seller_a_accused(fake)
    out = json.loads(await trace.scam_list(action="add", handle="@seller_a", note="我 10 月 20 日付了 120 元没发货"))
    assert out["status"] == "added" and out["total"] == 1 and out["record"]["tier"] == "D" and out["record"]["evidence_count"] == 3
    assert out["record"]["reporters"] == ["@jackchen"] and (roots / "downloads" / "scamlist.jsonl").exists()
    q = json.loads(await trace.scam_list(action="query", query=W1))
    assert q["matches"][0]["user_id"] == 1
    e = json.loads(await trace.scam_list(action="export", fmt="rose_csv"))
    assert e["records"] == 1 and open(e["file"], encoding="utf-8").read().startswith("id,reason")
    st = json.loads(await trace.scam_list(action="stats"))
    assert st["by_tier"] == {"D": 1} and st["reporters"] == ["@jackchen"]


async def test_tool_add_refuses_clean_seller_without_note(fake, roots):
    fake.users["seller_b"] = B
    fake.common = [GROUP]
    fake.by_user[(777, 2)] = [msg(1, "GPT Plus 120元 一年质保 全程售后", B)]

    async def get_me():
        return SimpleNamespace(id=100, username="jackchen")

    fake.get_me = get_me
    with pytest.raises(Exception, match="没有证据不入库"):
        await trace.scam_list(action="add", handle="@seller_b")


async def test_tool_merge_from_file_and_card_consults_library(fake, chain, roots):
    other = roots / "from_ak.jsonl"
    rec = scamlist.record_from_card({**CARD_D, "profile": {"id": 1, "kind": "user", "name": "胜山"}}, "@ak")
    other.write_text(json.dumps(rec, ensure_ascii=False) + "\n" + json.dumps({"user_id": 5, "evidence": []}) + "\n", encoding="utf-8")
    m = json.loads(await trace.scam_list(action="merge", files=[str(other)]))
    assert m["before"] == 0 and m["after"] == 1 and m["dropped_without_evidence"] == 1
    # 现在一个干干净净的卖家，只要跟库里的人共用收款地址，卡片直接 D
    fake.users["seller_c"] = C
    fake.common = [GROUP]
    fake.by_user[(777, 3)] = [msg(1, f"Claude 代充 150元 收款 {W1} 当天开通", C)]
    out = json.loads(await trace.check_seller(handle="@seller_c", probe_sites=False))
    assert out["tier"] == "D" and "共用收款地址" in out["red_flags"][0]
    # 库里本人：带库里的原话当证据
    fake.users["seller_a"] = A
    fake.by_user[(777, 1)] = [msg(2, "GPT Plus 120元 一年质保 全程售后", A)]
    out = json.loads(await trace.check_seller(handle="@seller_a", probe_sites=False))
    assert out["tier"] == "D" and out["red_flags"][0].startswith("骗子库里有记录：D 档，1 条证据，1 个举报人") and out["evidence"][0]["dimension"] == "骗子库"


async def test_tool_refuses_outside_roots(fake, roots):
    with pytest.raises(Exception, match="outside allowed roots"):
        await trace.scam_list(action="stats", library="/etc/passwd")
