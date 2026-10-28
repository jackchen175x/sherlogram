"""网站深挖：三个免费接口全用假的回复，不联网。"""
import json
import os
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

import pytest

from tg_mcp import domain_intel as di
from tg_mcp import trace
from tests.test_trace import SEED, SITES, A, GROUP, fake, msg  # noqa: F401

CRT = [
    {"name_value": "*.card1.shop\ncard1.shop", "not_before": "2026-10-20T00:00:00"},
    {"name_value": "card1.shop\npay.card9.top\nkami.card9.top", "not_before": "2026-09-01T00:00:00"},
    {"name_value": "\n".join(f"site{i}.example.com" for i in range(30)) + "\ncard1.shop", "not_before": "2025-01-01T00:00:00"},  # 共享证书
]
RDAP = {
    "entities": [
        {"roles": ["registrar"], "vcardArray": ["vcard", [["fn", {}, "text", "NameSilo, LLC"]]]},
        {"roles": ["registrant"], "vcardArray": ["vcard", [["fn", {}, "text", "DATA REDACTED"], ["org", {}, "text", "Demo Studio"], ["email", {}, "text", "boss@demo-fake.cn"]]]},
    ],
    "events": [{"eventAction": "registration", "eventDate": "2026-10-15T08:00:00Z"}],
    "nameservers": [{"ldhName": "NS1.DNSPOD.NET"}],
}


async def asyncio_noop():
    return None


def fake_get(responses):
    async def _get(url, params=None, timeout=25):
        for key, (status, body) in responses.items():
            if key in url:
                return SimpleNamespace(status_code=status, text=body if isinstance(body, str) else json.dumps(body), json=lambda b=body: b)
        raise AssertionError(url)
    return _get


def test_registrable():
    assert di.registrable("pay.ldxp.demo-fake.cn") == "demo-fake.cn" and di.registrable("a.b.example.com.cn") == "example.com.cn" and di.registrable("x.co.uk") == "x.co.uk"


async def test_deep_probe_merges_three_sources(monkeypatch):
    monkeypatch.setattr(di, "_get", fake_get({
        "crt.sh": (200, CRT),
        "hackertarget": (200, "card1.shop\nwww.card2.shop\npay.card9.top"),
        "rdap.org": (200, RDAP),
    }))
    d = await di.deep_probe("card1.shop")
    assert d["cert_siblings"] == ["card9.top"] and d["first_cert"] == "2026-09-01" and d["shared_certs_skipped"] == 1 and d["certs"] == 3
    assert d["same_ip"] == ["card2.shop", "card9.top"] and d["same_ip_count"] == 3
    assert d["registrar"] == "NameSilo, LLC" and d["registrant"] == "Demo Studio" and d["registrant_email"] == "boss@demo-fake.cn"
    assert d["created"] == "2026-10-15" and d["nameservers"] == ["ns1.dnspod.net"]
    assert "域名换过主人" in d["note"]  # 2026-09 就有证书，2026-10 才注册


async def test_deep_probe_tolerates_errors_and_shared_hosting(monkeypatch):
    monkeypatch.setattr(di, "_get", fake_get({
        "crt.sh": (502, "Bad Gateway"),
        "hackertarget": (200, "API count exceeded - Increase Quota with Membership"),
        "rdap.org": (404, {}),
    }))
    calls = []
    orig = di._get

    async def counting(url, params=None, timeout=25):
        calls.append(url)
        return await orig(url, params, timeout)

    monkeypatch.setattr(di, "_get", counting)
    monkeypatch.setattr(di.asyncio, "sleep", lambda *_: asyncio_noop())
    d = await di.deep_probe("card1.shop")
    assert sum("crt.sh" in c for c in calls) == 2  # 502 重试了一次
    assert d["cert_error"] == "http_502" and d["ip_note"].startswith("API count exceeded") and d["rdap_error"] == "http_404"
    monkeypatch.setattr(di, "_get", fake_get({"crt.sh": (200, []), "hackertarget": (200, "\n".join(f"s{i}.com" for i in range(40))), "rdap.org": (404, {})}))
    d = await di.deep_probe("card1.shop")
    assert d["same_ip"] == [] and "共享主机" in d["ip_note"]


@pytest.fixture
def deep(monkeypatch):
    async def probe(host):
        return {"card1.shop": {"cert_siblings": ["card9.top"], "same_ip": ["card2.shop"], "registrar": "NameSilo, LLC", "registrant_email": "boss@demo-fake.cn", "created": "2026-10-15"},
                "card2.shop": {"registrant_email": "boss@demo-fake.cn", "created": "2025-01-01"},
                "card9.top": {"first_cert": "2026-10-20"}}.get(host, {})
    monkeypatch.setattr(trace, "deep_probe", probe)


async def test_trace_deep_sites_links_siblings(fake, deep):
    fake.msgs["demo_group"] = SEED
    out = json.loads(await trace.trace_source(chat_id="demo_group", depth=2, deep_sites=2))
    assert out["sites"]["card1.shop"]["deep"]["cert_siblings"] == ["card9.top"]
    reasons = " ".join(" ".join(c["reasons"]) for c in out["candidates"])
    assert "同一张证书上" in reasons and "注册人跟它一样" in reasons
    assert "-->|同证书|" in out["mermaid"] and "-->|同注册人|" in out["mermaid"] and "card9.top" in out["mermaid"]


async def test_trace_from_site_seed(fake, deep):
    out = json.loads(await trace.trace_source(site="https://card1.shop/buy", depth=1))
    assert out["posts_read"] == 0 and "deep" in out["sites"]["card1.shop"] and out["sites"]["card1.shop"]["platform"] == "独角数卡"
    assert all(c["node"] != "card1.shop" for c in out["candidates"])


async def test_check_seller_flags_brand_new_domain(fake, deep):
    fake.users["seller_a"] = A
    fake.common = [GROUP]
    fake.by_user[(777, 1)] = [msg(1, "GPT Plus 120元 老店三年 卡网 card1.shop 秒发", A)]
    out = json.loads(await trace.check_seller(handle="@seller_a", deep_sites=True))
    assert out["tier"] == "C" and any("天前才注册" in f for f in out["red_flags"])
    assert out["sites"]["card1.shop"]["deep"]["registrar"] == "NameSilo, LLC"
