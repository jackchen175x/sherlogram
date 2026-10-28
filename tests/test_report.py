"""一键出报告：从溯源结果 / 卖家卡片生成网页。不连网络。"""
import json
import os

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

import pytest

import tg_mcp.app
from tg_mcp import report, trace
from tests.test_trace import SEED, A, GROUP, msg, fake, chain, W1, W3  # noqa: F401  复用假 client


@pytest.fixture
def roots(monkeypatch, tmp_path):
    monkeypatch.setattr(tg_mcp.app, "ALLOWED_ROOTS", [tmp_path])
    monkeypatch.setattr(tg_mcp.app, "HTTP_MODE", True)
    return tmp_path


async def test_trace_report_writes_html_with_graph(fake, roots):
    fake.msgs["demo_group"] = SEED
    out = json.loads(await trace.trace_source(chat_id="demo_group", depth=2, report=True))
    path = out["report"]
    assert path.startswith(str(roots)) and path.endswith(".html")
    page = open(path, encoding="utf-8").read()
    assert "const DATA = " in page and '"kind": "wallet"' not in page  # 这次没有钱包
    assert "@seller_a" in page and "card1.shop" in page and "api.upstream-demo.com" in page
    assert "线索汇聚点" in page and "vis-network" in page and "savePng" in page
    data = json.loads(page.split("const DATA = ", 1)[1].split(";</script>", 1)[0].replace("<\\/", "</"))
    kinds = {n["id"]: n for n in data["graph"]["nodes"]}
    assert kinds["@seller_a"]["kind"] == "seller" and kinds["card1.shop"]["kind"] == "site" and kinds["card1.shop"]["sub"] == "独角数卡"
    assert kinds["api.upstream-demo.com"]["source"] is True and kinds["@upstream_bot"]["url"] == "https://t.me/upstream_bot"
    assert any(e["label"] == "联系方式" for e in data["graph"]["edges"])


async def test_trace_report_refused_without_roots(fake, monkeypatch):
    monkeypatch.setattr(tg_mcp.app, "ALLOWED_ROOTS", [])
    monkeypatch.setattr(tg_mcp.app, "HTTP_MODE", True)
    fake.msgs["demo_group"] = SEED
    out = json.loads(await trace.trace_source(chat_id="demo_group", depth=1, report=True))
    assert out["report"].startswith("没写报告")


async def test_seller_report_card(fake, chain, roots):
    fake.users["seller_a"] = A
    fake.common = [GROUP]
    fake.by_user[(777, 1)] = [msg(1, f"GPT Plus 120元 收款 {W1} 秒发 售后无忧", A)]
    out = json.loads(await trace.check_seller(handle="@seller_a", probe_sites=False, report=True))
    page = open(out["report"], encoding="utf-8").read()
    assert 'class="tier tier-A"' in page and "钱的情况" in page and W3[:8] in page and 'id="graph"' not in page


def test_graph_from_mermaid_roundtrip(tmp_path):
    text = '''graph LR
  classDef seller fill:#f4f4f4,stroke:#999
  n0["@shopa_kefu<br/>ShopA唯一客服"]
  n1["@demo_channel<br/>GPT Plus批发源头"]:::seller
  n2["shopa.pro<br/>卡密站"]:::source
  n3["TNG3Gu…CkVH<br/>进 71.9万U · 出 675.6万U"]:::wallet
  n1 -->|贴链接| n2
  n2 -->|联系方式| n0
  n3 -->|转出 288.0万U/2笔| n2
'''
    g = report.graph_from_mermaid(text)
    by = {n["id"]: n for n in g["nodes"]}
    assert by["n0"]["kind"] == "handle" and by["n1"]["kind"] == "seller" and by["n2"]["kind"] == "site" and by["n2"]["source"]
    assert by["n3"]["kind"] == "wallet" and by["n3"]["sub"] == "进 71.9万U · 出 675.6万U"
    assert g["edges"][2] == {"from": "n3", "to": "n2", "label": "转出 288.0万U/2笔"}
    # 命令行：从保存的 JSON 生成网页
    src = tmp_path / "trace.json"
    src.write_text(json.dumps({"summary": "读了 3 条帖</script><b>x", "candidates": [], "mermaid": text}, ensure_ascii=False), encoding="utf-8")
    out = report.main([str(src)])
    page = out.read_text(encoding="utf-8")
    assert out.suffix == ".html" and "读了 3 条帖&lt;/script&gt;" in page and "<\\/" not in page.split("const DATA")[0]
