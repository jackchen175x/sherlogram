"""网站背后是谁：同一张证书上的其他域名、同一台服务器上的其他站、域名注册信息。

三个免费公开接口，都不用钥匙：crt.sh（证书透明日志）、HackerTarget（同 IP 反查，免费每天 20 次）、rdap.org（新式 whois）。
同 IP 反查是让对方按域名去查，所以本机走透明代理拿不到真实 IP 也不碍事。
"""

import asyncio
import re
from collections import Counter
from typing import Any, Dict, List, Optional

import httpx

from .common import GENERIC_HOSTS

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
SHARED_CERT_MAX = 20  # 一张证书上超过这么多域名 = CDN 给一堆客户合发的共享证书，不算同一个人
SHARED_IP_MAX = 25  # 同一 IP 上超过这么多站 = 共享主机 / CDN，不算同一台服务器
_SKIP_CERT = ("cloudflaressl.com", "sni.", "cdn.", "akamai", "fastly")
_REDACTED = re.compile(r"redact|privacy|proxy|whoisguard|protect|registrar|withheld|gdpr|not disclosed", re.I)
_TWO_LEVEL = ("com", "net", "org", "gov", "edu", "co", "ac")


def registrable(host: str) -> str:
    """粗略取主域名：最后两段；com.cn / co.uk 这类取三段。ponytail: 不引公共后缀表，灰产域名够用"""
    parts = host.casefold().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in _TWO_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


async def _get(url: str, params: Optional[Dict[str, Any]] = None, timeout: float = 25) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA}) as c:
        return await c.get(url, params=params)


async def cert_siblings(host: str) -> Dict[str, Any]:
    """证书透明日志：这个站的证书上还签了哪些别的域名（同一个人一张证书管几个站），以及最早的证书时间（站有多老）。"""
    r = await _get("https://crt.sh/", {"q": host, "output": "json"}, timeout=30)
    if r.status_code != 200:
        await asyncio.sleep(2)  # crt.sh 动不动 502，歇两秒再试一次
        r = await _get("https://crt.sh/", {"q": host, "output": "json"}, timeout=30)
    if r.status_code != 200:
        return {"cert_error": f"http_{r.status_code}"}
    rows = r.json()
    mine, siblings, skipped, dates = registrable(host), Counter(), 0, []
    for row in rows:
        names = {n.strip().casefold().lstrip("*.") for n in (row.get("name_value") or "").split("\n") if n.strip()}
        if len(names) > SHARED_CERT_MAX or any(s in n for n in names for s in _SKIP_CERT):
            skipped += 1
            continue
        if row.get("not_before"):
            dates.append(row["not_before"][:10])
        for n in names:
            other = registrable(n)
            if other != mine and other not in GENERIC_HOSTS:
                siblings[other] += 1
    return {
        "cert_siblings": [d for d, _ in siblings.most_common(10)], "certs": len(rows),
        "first_cert": min(dates) if dates else None, "shared_certs_skipped": skipped or None,
    }


async def same_ip(host: str) -> Dict[str, Any]:
    """同一台服务器上的其他站（HackerTarget 按域名反查，免费每天 20 次）。"""
    r = await _get("https://api.hackertarget.com/reverseiplookup/", {"q": host})
    text = r.text.strip()
    lines = [x.strip().casefold() for x in text.splitlines() if x.strip()]
    if r.status_code != 200 or not lines or any(" " in x for x in lines):  # 报错是一句话，正常结果是一行一个域名
        return {"ip_note": text[:100] or f"http_{r.status_code}"}
    mine = registrable(host)
    others = sorted({registrable(x) for x in lines if registrable(x) != mine and registrable(x) not in GENERIC_HOSTS})
    if len(lines) > SHARED_IP_MAX:
        return {"same_ip": [], "same_ip_count": len(lines), "ip_note": f"同一 IP 上有 {len(lines)} 个站，是共享主机或 CDN，不算同一台服务器"}
    return {"same_ip": others[:15], "same_ip_count": len(lines)}


def _vcard(entity: Dict[str, Any]) -> Dict[str, str]:
    out = {}
    for item in (entity.get("vcardArray") or ["", []])[1]:
        if len(item) >= 4 and item[0] in ("fn", "org", "email") and isinstance(item[3], str) and item[3] and not _REDACTED.search(item[3]):
            out.setdefault(item[0], item[3])
    return out


async def rdap(host: str) -> Dict[str, Any]:
    """域名注册信息：注册商、注册日期、注册人（多半打码）、域名服务器。"""
    dom = registrable(host)
    r = await _get(f"https://rdap.org/domain/{dom}")
    if r.status_code != 200:
        return {"rdap_error": f"http_{r.status_code}"}
    j = r.json()
    out: Dict[str, Any] = {"domain": dom}
    for e in j.get("entities") or []:
        roles, card = e.get("roles") or [], _vcard(e)
        if "registrar" in roles and card.get("fn"):
            out["registrar"] = card["fn"]
        if "registrant" in roles:
            who = card.get("org") or card.get("fn")
            if who:
                out["registrant"] = who
            if card.get("email"):
                out["registrant_email"] = card["email"].casefold()
    for ev in j.get("events") or []:
        if ev.get("eventAction") == "registration" and ev.get("eventDate"):
            out["created"] = ev["eventDate"][:10]
    ns = sorted({(n.get("ldhName") or "").casefold() for n in (j.get("nameservers") or []) if n.get("ldhName")})
    if ns:
        out["nameservers"] = ns
    return out


async def deep_probe(host: str) -> Dict[str, Any]:
    """三路一起查，哪路失败只记一个错，别拖垮整次。"""
    out: Dict[str, Any] = {}
    for name, res in zip(("cert", "ip", "rdap"), await asyncio.gather(cert_siblings(host), same_ip(host), rdap(host), return_exceptions=True)):
        if isinstance(res, BaseException):
            out[f"{name}_error"] = type(res).__name__
        else:
            out.update(res)
    if out.get("first_cert") and out.get("created") and out["first_cert"] < out["created"]:
        out["note"] = f"最早证书（{out['first_cert']}）比这次注册（{out['created']}）还早：域名换过主人，老口碑不作数"
    return out
