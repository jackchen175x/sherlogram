"""调研类工具：一键溯源（供应链图谱）、卖家一眼看（分档卡片）。

把「顺藤摸瓜找源头」四步法做成两次调用：
  搜帖 → 抽 @号 / 网址 → 抓网站找下一跳（站上留的联系方式、调用的接口、平台指纹、服务器）
  → 回 Telegram 核对 → 看线索汇聚到谁身上。
跑几分钟、产物是文件的大扫描仍在 market_watch.py；这里每次运行都有预算，超了在结果里明说。
"""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import httpx
from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from telethon import errors, functions, types, utils

from .app import client, logger, mcp
from .common import (
    ALIAS_PATTERNS,
    EMAIL_RE,
    GENERIC_HOSTS,
    NEGATIVE_WORDS,
    WECHAT_RE,
    ChatRef,
    ValidationError,
    dump_json,
    log_and_format_error,
    name_kin,
    norm_text,
    resolve_entity,
    validate_id,
    writable_path,
)
from .report import render_html
from .search import search_public_posts

# ---------- 文本里的线索 ----------

_HTTP_HOST = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})", re.I)
# 广告里的卡网常常不带 http（「卡网 abc.shop」）。只认常见后缀，免得把文件名当域名
_BARE_HOST = re.compile(
    r"(?<![\w@/.-])((?:[a-z0-9-]+\.)+(?:com|net|org|cn|me|io|co|shop|top|xyz|vip|cc|app|site|store|online"
    r"|club|tech|pro|ai|ru|us|uk|de|jp|hk|tw|sg|kr|in|fr|info|biz|tv|link|buzz|icu|fun|live|life|art|dev"
    r"|cloud|one|run|ws|la|sh|is|to|gg|ly|ink|wang|asia|ltd|group|chat|cool|win|work|zone))\b",
    re.I,
)
_AT_HANDLE = re.compile(r"(?<![\w.@/])@([A-Za-z0-9_]{5,32})\b(?!\.[A-Za-z])")  # 后面紧跟 .com 的是邮箱不是人
_TME_HANDLE = re.compile(r"(?:t\.me/|telegram\.me/|tg://resolve\?domain=)([A-Za-z0-9_]{5,32})", re.I)
_NOT_HANDLES = {
    "joinchat", "addstickers", "addemoji", "addtheme", "setlanguage", "share", "proxy", "socks", "login",
    "gmail", "outlook", "hotmail", "icloud", "protonmail", "yahoo", "foxmail",  # 「邮箱 @outlook.com」里的不是人
}
# 网站代码里到处都是的公共件（统计、字体、CDN、验证码），不是上游
_CDN_HOSTS = (
    "googleapis.com", "gstatic.com", "google-analytics.com", "googletagmanager.com", "jsdelivr.net",
    "unpkg.com", "cloudflare.com", "cloudflareinsights.com", "bootcdn.net", "bootcss.com", "staticfile.org",
    "staticfile.net", "jquery.com", "fontawesome.com", "w3.org", "schema.org", "baidu.com", "cnzz.com",
    "51.la", "recaptcha.net", "hcaptcha.com", "gravatar.com", "facebook.net", "github.io",
    "githubusercontent.com", "wp.com", "wordpress.org", "apple.com", "microsoft.com", "alicdn.com",
    "qq.com", "weixin.qq.com", "umeng.com", "clarity.ms", "vercel.app", "cloudfront.net", "amazonaws.com",
    "tailwindcss.com", "simpleicons.org", "shields.io", "mozilla.org", "fastly.net", "akamaihd.net",
    "googleusercontent.com", "ytimg.com", "wa.me", "whatsapp.com", "line.me", "instagram.com", "facebook.com",
    "tiktok.com", "linkedin.com", "reddit.com", "discord.gg", "discord.com", "stripe.com", "paypal.com", "alipay.com",
)
# 域名第一段是这些的，是静态资源不是业务后台（cdn.xxx.com、static.xxx.com）
_STATIC_LABELS = {"cdn", "cdn1", "cdn2", "static", "assets", "img", "images", "image", "fonts", "font", "js", "css", "media", "s3", "cache"}
# 卡网 / 发卡平台的指纹：命中就知道这家店是哪套系统搭的。同一套系统的多家店往往同一个上游
_PLATFORM_MARKERS = (
    ("dujiaoka", "独角数卡"), ("独角数卡", "独角数卡"), ("链动小铺", "链动小铺"), ("acg-faka", "异次元发卡"),
    ("异次元", "异次元发卡"), ("zovocard", "ZovoCard 卡台"), ("woocommerce", "WooCommerce"),
    ("shopify", "Shopify"), ("发卡", "发卡站"), ("卡密", "卡密站"),
)
_PRICE = re.compile(r"\d+(?:\.\d+)?\s*(?:r\b|rmb|元|块|¥|刀|u\b|usd|美金)", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAGS = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.I | re.S)
# 必须带浏览器 UA：实测有的卡网对默认 UA 返回 403，会把正规站误判成挂了
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_SCAN_FACTOR = 10  # 为了凑够 max_messages 条、每人不超 _PER_SENDER 条，最多往回翻这么多倍的原始消息
_PER_SENDER = 5  # 种子帖里每个发帖人最多留几条：实测搜「卡网」60 条全是同一个刷屏机器人，别让他独占样本
_PAUSE = 1.0  # 两次 @用户名 解析之间停这么久：ResolveUsername 是已知的封号源（出过 18 小时限速）


def _clean_host(h: str) -> str:
    h = h.casefold().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def _is_generic(h: str) -> bool:
    return h.split(".")[0] in _STATIC_LABELS or any(h == g or h.endswith("." + g) for g in (*GENERIC_HOSTS, *_CDN_HOSTS))


def hosts_in(text: str) -> Set[str]:
    """一段文字里的网站域名（带不带 http 都认），去掉社交平台、CDN 这些串不出意义的。"""
    found = {_clean_host(h) for h in _HTTP_HOST.findall(text)} | {_clean_host(h) for h in _BARE_HOST.findall(text)}
    return {h for h in found if not _is_generic(h)}


def handles_in(text: str) -> Set[str]:
    """一段文字里的 Telegram @号（含 t.me/xxx 这种写法），小写。"""
    found = {h.casefold() for h in _AT_HANDLE.findall(text)} | {h.casefold() for h in _TME_HANDLE.findall(text)}
    return found - _NOT_HANDLES


def site_clues(host: str, body: str) -> Dict[str, Any]:
    """从一个站的首页源码里抽下一跳：留的 Telegram 联系方式、调用的别家域名、是哪套平台。"""
    low = body.casefold()
    m = _TITLE.search(body)
    title = re.sub(r"\s+", " ", m.group(1)).strip()[:60] if m else ""
    # @号 只在可见文字里找：脚本和样式里的 @media / @import 不是人
    visible = _TAGS.sub(" ", body)
    contacts = {h.casefold() for h in _TME_HANDLE.findall(body)} | {h.casefold() for h in _AT_HANDLE.findall(visible)}
    calls: Counter = Counter()
    for h in _HTTP_HOST.findall(body):
        h = _clean_host(h)
        if h == host or h.endswith("." + host) or host.endswith("." + h) or _is_generic(h):
            continue
        calls[h] += 1
    platform = next((name for marker, name in _PLATFORM_MARKERS if marker in low), None)
    # 调得最多的排前面：真正的业务后台在页面里出现几十次，顺手挂的外链只有一次
    return {"title": title, "contacts": sorted(contacts - _NOT_HANDLES)[:10], "calls": [h for h, _ in calls.most_common(8)], "platform": platform}


# ---------- 收款地址、链上流水 ----------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ETH_ADDR = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
_BTC_ADDR = re.compile(r"\bbc1[a-z0-9]{25,62}\b")
_TRON_ADDR = re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b")
TRONGRID = "https://api.trongrid.io"
USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # 波场上 USDT 的合约地址，灰产收款几乎全走它
_TX_LIMIT = 200  # TronGrid 一页最多 200 笔


def valid_tron(addr: str) -> bool:
    """波场地址要过校验和：34 个字符里随便一个错都不是地址，免得把乱码当收款地址。"""
    n = 0
    for ch in addr:
        i = _B58.find(ch)
        if i < 0:
            return False
        n = n * 58 + i
    try:
        raw = n.to_bytes(25, "big")
    except OverflowError:
        return False
    return raw[0] == 0x41 and hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] == raw[21:]


def wallets_in(text: str) -> Set[Tuple[str, str]]:
    """一段文字里的收款地址：(链, 地址)。波场过校验和；以太、比特币只按格式。"""
    found = {("tron", a) for a in _TRON_ADDR.findall(text) if valid_tron(a)}
    found |= {("eth", a.lower()) for a in _ETH_ADDR.findall(text)}
    found |= {("btc", a) for a in _BTC_ADDR.findall(text)}
    return found


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def _usdt(n: float) -> str:
    return f"{n / 10000:.1f}万U" if n >= 10000 else f"{n:.0f}U"


async def _tron_transfers(addr: str, limit: int = _TX_LIMIT) -> List[Dict[str, Any]]:
    """波场官方免费接口拉这个地址最近的 USDT 转账。不带钥匙也能查，查多了会限速；配 TRONGRID_API_KEY 放宽。"""
    headers = {"User-Agent": UA}
    if os.getenv("TRONGRID_API_KEY"):
        headers["TRON-PRO-API-KEY"] = os.environ["TRONGRID_API_KEY"]
    async with httpx.AsyncClient(timeout=20, headers=headers) as c:
        r = await c.get(f"{TRONGRID}/v1/accounts/{addr}/transactions/trc20", params={"limit": limit, "contract_address": USDT_TRC20})
    if r.status_code != 200:
        raise RuntimeError(f"trongrid_{r.status_code}")
    return r.json().get("data", [])


def summarize_transfers(addr: str, txs: List[Dict[str, Any]], top: int = 3) -> Dict[str, Any]:
    """一个地址的 USDT 进出账：总额、笔数、首末时间、进出对手方前几名（按金额）。"""
    ins: Dict[str, List[float]] = defaultdict(list)
    outs: Dict[str, List[float]] = defaultdict(list)
    stamps = []
    for t in txs:
        if t.get("type", "Transfer") != "Transfer":
            continue  # 「授权」记录的金额是 2^256-1 这种天文数字，不是钱动了
        try:
            amt = int(t.get("value", 0)) / 10 ** int(t.get("token_info", {}).get("decimals", 6))
        except (TypeError, ValueError):
            continue
        if amt > 1e11:
            continue  # 比全世界的 USDT 还多，肯定不是转账
        if t.get("to") == addr:
            ins[t.get("from", "?")].append(amt)
        elif t.get("from") == addr:
            outs[t.get("to", "?")].append(amt)
        if t.get("block_timestamp"):
            stamps.append(int(t["block_timestamp"]) // 1000)

    def ranked(d):
        rows = sorted(((k, sum(v), len(v)) for k, v in d.items()), key=lambda x: -x[1])[:top]
        return [{"address": k, "usdt": round(a, 2), "count": n} for k, a, n in rows]

    def day(ts):
        return f"{datetime.fromtimestamp(ts, timezone.utc):%Y-%m-%d}"

    return {
        "tx": len(txs), "in_usdt": round(sum(map(sum, ins.values())), 2), "out_usdt": round(sum(map(sum, outs.values())), 2),
        "payers": len(ins), "payees": len(outs),
        "first": day(min(stamps)) if stamps else None, "last": day(max(stamps)) if stamps else None,
        "top_in": ranked(ins), "top_out": ranked(outs),
        "note": f"只看了最近 {_TX_LIMIT} 笔" if len(txs) >= _TX_LIMIT else None,
        "explorer": f"https://tronscan.org/#/address/{addr}",  # 标签（交易所 / 风险）只能上这儿手查，免费接口不给
    }


# ---------- 抓网站 ----------


def _public_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


# 透明代理（Shadowrocket / Clash 的 fake-ip 模式）把所有域名都解析到这一段，真实 IP 只有代理那头知道
_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


def _ip_class(ips: List[str]) -> str:
    """public / fakeip / private。全落在假 IP 段 = 本机走代理，放行但拿不到真实 IP（判不了同服务器）。"""
    try:
        objs = [ipaddress.ip_address(i) for i in ips]
    except ValueError:
        return "private"
    if objs and all(o in _FAKE_IP for o in objs):
        # ponytail: fake-ip 下「域名指向内网」的检查做不了，剩余风险只是对内网发一次 GET /，响应不回传；
        # 字面的内网 IP 仍然拒绝。真要堵死就得自己向 1.1.1.1 查 DNS，出事再加
        return "fakeip"
    return "public" if objs and all(o.is_global for o in objs) else "private"


async def _resolve_ips(host: str) -> List[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except Exception:
        return []
    return sorted({i[4][0] for i in infos})


async def _refuse_private(request: httpx.Request) -> None:
    """每一跳（含跳转）都得过这道门：群里一条消息不该把本机当跳板去碰内网。"""
    if _ip_class(await _resolve_ips(request.url.host)) == "private":
        raise httpx.ConnectError(f"refused non-public host {request.url.host}")


async def probe_site(host: str) -> Dict[str, Any]:
    """抓首页拿下一跳线索。失败叫 probe_failed 而不是 unreachable：那多半是本机网络的事，不是对站的判决。"""
    rec: Dict[str, Any] = {"host": host, "verdict": "probe_failed"}
    ips = await _resolve_ips(host)
    if not ips:
        rec["error"] = "dns"
        return rec
    cls = _ip_class(ips)
    if cls == "private":
        rec["error"] = "non_public_ip"
        return rec
    if cls == "fakeip":
        rec["note"] = "本机走代理，拿不到真实 IP，判不了同服务器"
    else:
        rec["ip"] = ips[0]
    r = None
    for scheme in ("https", "http"):
        try:
            async with httpx.AsyncClient(
                timeout=12, follow_redirects=True, max_redirects=5, headers={"User-Agent": UA},
                event_hooks={"request": [_refuse_private]},
            ) as c:
                r = await c.get(f"{scheme}://{host}/")
            break
        except Exception as e:
            rec["error"] = type(e).__name__
    if r is None:
        return rec
    rec.pop("error", None)
    body = r.text[:300_000]
    rec["status"] = r.status_code
    final = _clean_host(r.url.host or host)
    if final != host:
        rec["redirects_to"] = final
    if "cf-ray" in r.headers or "cloudflare" in r.headers.get("server", "").casefold():
        rec["cdn"] = "cloudflare"  # 同一个 IP 背后是成千上万个站，不能拿来判「同一台服务器」
        # ponytail: 只认 Cloudflare；别的 CDN 会把不相干的站误报成同服务器，出现了再加
    if "suspected phishing" in body.casefold():
        rec["verdict"] = "cloudflare_phishing"  # 第三方权威给出的诈骗认定，跟「服务器挂了」含义相反
    elif r.status_code < 400:
        rec["verdict"] = "reachable"
    else:
        rec["verdict"] = f"http_{r.status_code}"
    if "html" in r.headers.get("content-type", "").casefold() and not _is_generic(final):
        # 不是网页（脚本、样式、图片）或者跳去了公共平台（github.com）的，页面里抽不出这家店自己的线索
        rec.update(site_clues(final, body))
    return rec


# ---------- Telegram 那头 ----------


def _entity_key(ent) -> Tuple[str, str, str]:
    """(节点键, 显示名, 类型)。有用户名用 @用户名，没有就用编号。"""
    uname = getattr(ent, "username", None)
    if hasattr(ent, "first_name"):
        kind = "bot" if getattr(ent, "bot", False) else "user"
        label = " ".join(p for p in (ent.first_name, getattr(ent, "last_name", None)) if p) or uname or f"id {ent.id}"
    elif hasattr(ent, "title"):
        kind = "group" if getattr(ent, "megagroup", False) or not hasattr(ent, "broadcast") else ("channel" if ent.broadcast else "group")
        label = ent.title
    else:
        kind, label = "unknown", str(getattr(ent, "id", "?"))
    key = f"@{uname.casefold()}" if uname else f"id:{ent.id}"
    return key, label, kind


def _chat_label(msg, fallback: str = "?") -> str:
    c = getattr(msg, "chat", None)
    return getattr(c, "title", None) or getattr(c, "first_name", None) or str(getattr(msg, "chat_id", None) or fallback)


def _fwd_key(msg) -> Tuple[Optional[str], Optional[str]]:
    fwd = getattr(msg, "fwd_from", None)
    if not fwd:
        return None, None
    forward = getattr(msg, "forward", None)
    src = getattr(forward, "chat", None) or getattr(forward, "sender", None)
    if src is not None:
        key, label, _ = _entity_key(src)
        return key, label
    if getattr(fwd, "from_id", None):
        pid = utils.get_peer_id(fwd.from_id)
        return f"id:{pid}", str(pid)
    if getattr(fwd, "from_name", None):
        return f"name:{fwd.from_name}", fwd.from_name  # 隐藏身份的转发，只有个名字，解析不了
    return None, None


def _post(msg, chat_label: str) -> Dict[str, Any]:
    """一条消息压成调研要的几样：谁发的、说了什么、转发自哪、是不是老帖翻新。"""
    sender = getattr(msg, "sender", None)
    if sender is not None:
        key, label, kind = _entity_key(sender)
    else:
        pid = getattr(msg, "sender_id", None) or utils.get_peer_id(msg.peer_id)
        key, label, kind = f"id:{pid}", str(pid), "unknown"
    fwd = getattr(msg, "fwd_from", None)
    edit = getattr(msg, "edit_date", None)
    fkey, flabel = _fwd_key(msg)
    return {
        "id": msg.id, "date": msg.date, "edit_date": edit, "chat": chat_label, "text": msg.message or "",
        "sender": key, "sender_label": label, "sender_kind": kind,
        "fwd": fkey, "fwd_label": flabel,
        # 转发 30 天前的老广告冒充今日现货；把老帖改个价当新帖 —— 两种翻新手法
        "fwd_old": bool(fwd and getattr(fwd, "date", None) and (msg.date - fwd.date).days >= 30),
        "edited_old": bool(edit and (edit - msg.date).days >= 7),
        "via_bot": bool(getattr(msg, "via_bot_id", None)),
    }


async def _seed_posts(entity, query: Optional[str], limit: int, label_of) -> List[Dict[str, Any]]:
    """读种子帖：每个发帖人最多 _PER_SENDER 条，最多翻 limit×5 条原始消息。entity 为 None 时是全局搜索。"""
    kept: List[Dict[str, Any]] = []
    per: Counter = Counter()
    # 没给关键词就只读带链接的帖：供应链的线索全在链接里，而且服务器能直接筛（7 个群十几万条里只有 2829 条带链接）
    kw = {"search": query} if query else {"filter": types.InputMessagesFilterUrl()}
    async for m in client.iter_messages(entity, limit=limit * _SCAN_FACTOR, **kw):
        p = _post(m, label_of(m))
        if per[p["sender"]] >= _PER_SENDER:
            continue
        per[p["sender"]] += 1
        kept.append(p)
        if len(kept) >= limit:
            break
    return kept


async def _public_posts(query: str, limit: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """全网公开频道的帖子（走 search_public_posts，它自己管每日免费额度、绝不花星星）。"""
    data = json.loads(await search_public_posts(query=query, limit=min(limit, 50)))
    if data.get("status") != "ok":
        return [], data.get("status")
    posts = []
    for p in data["posts"]:
        c = p.get("channel") or {}
        key = f"@{c['username'].casefold()}" if c.get("username") else f"id:{c.get('id')}"
        posts.append({
            "id": p["id"], "date": p.get("date"), "edit_date": None, "chat": "公开频道", "text": p.get("text") or "",
            "sender": key, "sender_label": c.get("title") or key, "sender_kind": "channel",
            "fwd": None, "fwd_label": None, "fwd_old": False, "edited_old": False, "via_bot": False,
        })
    return posts, None


# ---------- 图 ----------


class _Graph:
    def __init__(self):
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: Set[Tuple[str, str, str]] = set()
        self.weights: Dict[Tuple[str, str], str] = {}  # 画图时边上的附注（转了多少钱、几笔）

    def add(self, key: str, kind: str, label: Optional[str] = None, depth: int = 0, **info) -> Dict[str, Any]:
        n = self.nodes.setdefault(key, {"key": key, "kind": kind, "label": label or key.lstrip("@"), "depth": depth})
        if n["kind"] in ("handle", "unknown") and kind not in ("handle", "unknown"):
            n["kind"], n["label"] = kind, label or n["label"]  # 解析过了：从「不知道是啥」变成用户 / 频道
        n.update({k: v for k, v in info.items() if v})
        return n

    def link(self, src: str, dst: str, rel: str) -> None:
        if src != dst:
            self.edges.add((src, dst, rel))

    def referrers(self, key: str) -> Dict[str, Set[str]]:
        """谁指向这个节点：关系 → {来源}。"""
        out: Dict[str, Set[str]] = defaultdict(set)
        for s, d, rel in self.edges:
            if d == key:
                out[rel].add(s)
        return out

    def absorb(self, src: str, text: str, depth: int, fwd: Tuple[Optional[str], Optional[str]] = (None, None),
               rel: str = "贴链接") -> List[Tuple[str, str]]:
        """把一段文字里的线索记进图，返回新发现的 (键, 类型) 供下一层追。"""
        new = []
        for h in hosts_in(text):
            if h not in self.nodes:
                new.append((h, "site"))
            self.add(h, "site", h, depth)
            self.link(src, h, rel)
        for u in handles_in(text):
            k = f"@{u}"
            if k == src:
                continue
            if k not in self.nodes:
                new.append((k, "handle"))
            self.add(k, "handle", u, depth)
            self.link(src, k, "提到")
        if fwd[0]:
            if fwd[0] not in self.nodes:
                new.append((fwd[0], "handle"))
            self.add(fwd[0], "handle", fwd[1], depth)
            self.link(src, fwd[0], "转发自")
        for chain, addr in wallets_in(text):
            if addr not in self.nodes:
                new.append((addr, "wallet"))
            self.add(addr, "wallet", _short(addr), depth, chain=chain)
            self.link(src, addr, "收款地址")  # 两个卖家用同一个地址收款 = 同一个人，比任何文字线索都硬
        return new


async def _expand_handle(graph: _Graph, key: str, depth: int, max_posts: int = 20) -> List[Tuple[str, str]]:
    """回 Telegram 核对一个 @号：是人、机器人还是频道；频道就读它最近的帖，人就看简介。"""
    ent = await resolve_entity(key[1:] if key.startswith("@") else int(key[3:]))
    _, label, kind = _entity_key(ent)
    node = graph.add(key, kind, label, flags=[f for f in ("scam", "fake", "verified") if getattr(ent, f, False)])
    new: List[Tuple[str, str]] = []
    if kind in ("channel", "group"):
        msgs = await client.get_messages(ent, limit=max_posts)
        node["posts"] = len(msgs)
        for m in msgs:
            p = _post(m, label)
            src = key
            if kind == "group" and p["sender"] != key:
                if not (hosts_in(p["text"]) or handles_in(p["text"]) or p["fwd"]):
                    continue  # 没线索的闲聊成员不画进图，否则一个群就是几十个孤零零的点
                # 群里的帖是成员发的，不是群发的：新冒出来的卖家也记上
                graph.add(p["sender"], p["sender_kind"], p["sender_label"], depth + 1)
                graph.link(p["sender"], key, "在群里发帖")
                src = p["sender"]
            new += graph.absorb(src, p["text"], depth + 1, (p["fwd"], p["fwd_label"]))
    else:
        try:
            full = await client(functions.users.GetFullUserRequest(id=ent))
            about = getattr(full.full_user, "about", None) or ""
        except Exception as e:
            logger.warning(f"GetFullUser failed for {key}: {e}")
            about = ""
        if about:
            node["bio"] = about[:120]
            new += graph.absorb(key, about, depth + 1, rel="简介里写着")
    return new


async def _expand_wallet(graph: _Graph, addr: str, depth: int, branch: int = 3) -> List[Tuple[str, str]]:
    """查一个波场地址的 USDT 流水，把钱的来路和去向画进图；返回「钱转去了哪」的前几个地址供下一层追。"""
    node = graph.nodes[addr]
    if node.get("chain") != "tron":
        node["note"] = "只查波场 USDT，别的链先不追"
        return []
    info = summarize_transfers(addr, await _tron_transfers(addr))
    node.update({k: v for k, v in info.items() if v not in (None, [], 0)})
    new = []
    for row in info["top_in"]:
        graph.add(row["address"], "wallet", _short(row["address"]), depth + 1, chain="tron")
        graph.link(row["address"], addr, "转入")
        graph.weights[(row["address"], addr)] = f"{_usdt(row['usdt'])}/{row['count']}笔"
    for row in info["top_out"][:branch]:
        if row["address"] not in graph.nodes:
            new.append((row["address"], "wallet"))
        graph.add(row["address"], "wallet", _short(row["address"]), depth + 1, chain="tron")
        graph.link(addr, row["address"], "转出")
        graph.weights[(addr, row["address"])] = f"{_usdt(row['usdt'])}/{row['count']}笔"
    return new


async def _crawl(graph: _Graph, frontier: List[Tuple[str, str]], depth: int, budget: Dict[str, Any]) -> List[str]:
    """一层层往上追。每层先查站（并行）再核对 @号（串行、隔 1 秒）。预算用完或限速就停，并把停在哪说清楚。"""
    notes: List[str] = []
    sites_left, resolve_left, deadline = budget["sites"], budget["resolve"], budget["deadline"]
    wallets_left = budget.get("wallets", 0)
    sem = asyncio.Semaphore(4)

    async def probe(h):
        async with sem:
            return await probe_site(h)

    def rank(k):  # 被指得多的先查
        return -sum(len(v) for v in graph.referrers(k).values())

    for level in range(1, depth + 1):
        if not frontier:
            break
        if time.monotonic() > deadline:
            notes.append(f"时间到，停在第 {level} 层")
            break
        seen = set()
        frontier = [f for f in frontier if not (f[0] in seen or seen.add(f[0]))]
        sites = sorted((k for k, kind in frontier if kind == "site" and "checked" not in graph.nodes[k]), key=rank)
        handles = sorted(
            (k for k, kind in frontier if kind == "handle" and k.startswith(("@", "id:")) and "checked" not in graph.nodes[k]),
            key=rank,
        )
        if len(sites) > sites_left:
            notes.append(f"第 {level} 层还有 {len(sites) - sites_left} 个站没查（max_sites 用完）")
            sites = sites[:sites_left]
        if len(handles) > resolve_left:
            notes.append(f"第 {level} 层还有 {len(handles) - resolve_left} 个 @号 没核对（max_resolve 用完）")
            handles = handles[:resolve_left]
        wallets = sorted((k for k, kind in frontier if kind == "wallet" and "checked" not in graph.nodes[k]), key=rank)
        if len(wallets) > wallets_left:
            notes.append(f"第 {level} 层还有 {len(wallets) - wallets_left} 个钱包没查（max_wallets 用完）")
            wallets = wallets[:wallets_left]
        sites_left -= len(sites)
        resolve_left -= len(handles)
        wallets_left -= len(wallets)

        nxt: List[Tuple[str, str]] = []
        for rec in await asyncio.gather(*(probe(h) for h in sites)):
            node = graph.nodes[rec["host"]]
            node.update({k: v for k, v in rec.items() if k != "host" and v})
            node["checked"] = True
            if rec["verdict"] == "cloudflare_phishing":
                node["flags"] = ["钓鱼"]
            for c in rec.get("contacts", []):
                k = f"@{c}"
                if k not in graph.nodes:
                    nxt.append((k, "handle"))
                graph.add(k, "handle", c, level + 1)
                graph.link(rec["host"], k, "联系方式")
            for c in rec.get("calls", []):
                if c not in graph.nodes:
                    nxt.append((c, "site"))
                graph.add(c, "site", c, level + 1)
                graph.link(rec["host"], c, "调用")
            if rec.get("redirects_to"):
                t = rec["redirects_to"]
                if t not in graph.nodes:
                    nxt.append((t, "site"))
                graph.add(t, "site", t, level + 1)
                graph.link(rec["host"], t, "跳转")
        for k in handles:
            if time.monotonic() > deadline:
                notes.append(f"时间到，第 {level} 层的 @号 没核对完")
                break
            graph.nodes[k]["checked"] = True
            try:
                nxt += await _expand_handle(graph, k, level)
            except errors.FloodWaitError as e:
                notes.append(f"Telegram 限速 {e.seconds} 秒，停止核对 @号（别重试，等待会叠加）")
                break
            except Exception as e:
                graph.nodes[k]["error"] = type(e).__name__
            await asyncio.sleep(_PAUSE)
        for k in wallets:
            graph.nodes[k]["checked"] = True
            try:
                nxt += await _expand_wallet(graph, k, level)
            except Exception as e:
                graph.nodes[k]["error"] = str(e) if str(e).startswith("trongrid_") else type(e).__name__
                if "trongrid_4" in str(e):
                    notes.append("波场接口限速或拒绝，剩下的钱包没查（配 TRONGRID_API_KEY 可放宽）")
                    break
            await asyncio.sleep(_PAUSE / 3)
        frontier = nxt

    # 两个站在同一台服务器 = 大概率同一个人（Cloudflare 后面的除外，那个 IP 是共享的）
    by_ip: Dict[str, List[str]] = defaultdict(list)
    for k, n in graph.nodes.items():
        if n["kind"] == "site" and n.get("ip") and not n.get("cdn"):
            by_ip[n["ip"]].append(k)
    for hosts in by_ip.values():
        for a in hosts:
            for b in hosts:
                if a < b:
                    graph.link(a, b, "同服务器")
    return notes


_REASON = {
    "贴链接": "{n} 个卖家贴它的链接", "提到": "{n} 个账号提到它", "转发自": "{n} 个账号转发它的帖子",
    "联系方式": "{n} 个站留它当联系方式", "调用": "{n} 个站的代码调用它", "跳转": "{n} 个站跳转到它",
    "简介里写着": "{n} 个账号简介里写着它",
    "收款地址": "{n} 个卖家用它收款（同一个人）", "转出": "{n} 个钱包的钱转到它（资金汇聚）", "转入": "{n} 个地址给它打过钱",
}


def _candidates(graph: _Graph, sellers: Set[str]) -> List[Dict[str, Any]]:
    """线索汇聚到谁身上：被 ≥2 个不同来源指向的节点，按被指次数、再按层数（越深越上游）排。"""
    out = []
    for k, n in graph.nodes.items():
        if k in sellers:
            continue
        refs = graph.referrers(k)
        same = refs.pop("同服务器", set()) | {d for s, d, rel in graph.edges if s == k and rel == "同服务器"}
        members = refs.pop("在群里发帖", set())  # 成员在群里发帖是群的日常，不说明群是源头
        srcs = set().union(*refs.values()) - {k} if refs else set()
        if len(srcs) < 2:
            continue
        reasons = [_REASON[rel].format(n=len(s)) for rel, s in sorted(refs.items())]
        if members:
            reasons.append(f"{len(members)} 个带线索的账号在里面发帖")
        if same:
            reasons.append("与 " + "、".join(sorted(same)) + " 在同一台服务器")
        if n.get("platform"):
            reasons.append(f"平台：{n['platform']}")
        if n["kind"] == "wallet" and n.get("tx"):
            reasons.append(f"链上进 {_usdt(n.get('in_usdt', 0))}、出 {_usdt(n.get('out_usdt', 0))}（{n['tx']} 笔，{n.get('first')}～{n.get('last')}）")
        if n.get("flags"):
            reasons.append("标记：" + "、".join(n["flags"]))
        out.append({"node": k, "kind": n["kind"], "label": n["label"], "score": len(srcs), "depth": n["depth"], "reasons": reasons})
    out.sort(key=lambda c: (-c["score"], -c["depth"], c["node"]))
    return out[:5]


def _mermaid(graph: _Graph, candidates: List[Dict[str, Any]], sellers: Set[str], cap: int = 60) -> str:
    """GitHub 直接能渲染的图：橙色粗框 = 线索汇聚点，红 = 钓鱼 / 官方标记，灰 = 卖家，蓝 = 网站。"""
    top = {c["node"] for c in candidates}
    degree: Counter = Counter()
    for s, d, _ in graph.edges:
        degree[s] += 1
        degree[d] += 1
    keep = set(sorted(graph.nodes, key=lambda k: (k not in top, k not in sellers, -degree[k], k))[:cap])
    ids = {k: f"n{i}" for i, k in enumerate(sorted(keep))}
    lines = [
        "graph LR",
        "  classDef seller fill:#f4f4f4,stroke:#999",
        "  classDef site fill:#e8f1ff,stroke:#3b82f6",
        "  classDef source fill:#ffedd5,stroke:#ea580c,stroke-width:3px",
        "  classDef bad fill:#fee2e2,stroke:#dc2626",
        "  classDef wallet fill:#ede9fe,stroke:#7c3aed",
    ]
    for k in sorted(keep):
        n = graph.nodes[k]
        short = (n["label"] or "")[:24]
        text = k if k.startswith("@") else short
        if k.startswith("@") and short and short.casefold() != k[1:]:
            text += f"<br/>{short}"
        extra = n.get("platform") or ("钓鱼" if "钓鱼" in n.get("flags", []) else "")
        if n["kind"] == "wallet":
            extra = f"进 {_usdt(n.get('in_usdt', 0))} · 出 {_usdt(n.get('out_usdt', 0))}" if n.get("tx") else "钱包"
        if extra:
            text += f"<br/>{extra}"
        text = text.replace('"', "'").replace("|", "/")
        cls = "source" if k in top else "bad" if n.get("flags") else "seller" if k in sellers else "site" if n["kind"] == "site" else "wallet" if n["kind"] == "wallet" else ""
        lines.append(f'  {ids[k]}["{text}"]' + (f":::{cls}" if cls else ""))
    for s, d, rel in sorted(graph.edges):
        if s in keep and d in keep:
            w = graph.weights.get((s, d))
            lines.append(f"  {ids[s]} -->|{rel + (' ' + w if w else '')}| {ids[d]}")
    if len(graph.nodes) > cap:
        lines.append(f"  %% 还有 {len(graph.nodes) - cap} 个节点没画")
    return "\n".join(lines)


def _graph_payload(graph: _Graph, cands: List[Dict[str, Any]], sellers: Set[str]) -> Dict[str, list]:
    """给报告网页用的节点和边：类型、是否汇聚点、是否有红标、点开去哪。"""
    top = {c["node"] for c in cands}
    nodes = []
    for k, n in graph.nodes.items():
        kind = "seller" if k in sellers else "site" if n["kind"] == "site" else "wallet" if n["kind"] == "wallet" else "handle"
        label = k if k.startswith("@") or kind == "site" else n["label"]
        sub = "" if label == n["label"] or kind == "site" else n["label"][:24]
        if kind == "wallet":
            label, sub = _short(k), (f"进 {_usdt(n.get('in_usdt', 0))} · 出 {_usdt(n.get('out_usdt', 0))}" if n.get("tx") else "")
        elif n.get("platform"):
            sub = n["platform"]
        url = f"https://{k}" if kind == "site" else n.get("explorer") if kind == "wallet" else f"https://t.me/{k[1:]}" if k.startswith("@") else None
        nodes.append({"id": k, "label": label, "sub": sub, "kind": kind, "source": k in top, "bad": bool(n.get("flags")), "url": url})
    edges = [{"from": a, "to": b, "label": rel + (" " + graph.weights[(a, b)] if (a, b) in graph.weights else "")} for a, b, rel in sorted(graph.edges)]
    return {"nodes": nodes, "edges": edges}


async def _write_report(name: str, html_text: str, ctx: Optional[Context]) -> str:
    """报告只许写进启动时给的目录（跟发文件、下载同一条安全线）。返回路径或原因。"""
    path, err = await writable_path(None, name, ctx)
    if err:
        return f"没写报告：{err}"
    path.write_text(html_text, encoding="utf-8")
    return str(path)


def _slug(x: Any) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "-", str(x)).strip("-")[:40] or "report"


# ---------- 工具一：一键溯源 ----------


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Trace Source", openWorldHint=True, readOnlyHint=True))
@validate_id("chat_id")
async def trace_source(
    query: Optional[str] = None,
    chat_id: Optional[Union[ChatRef, List[ChatRef]]] = None,
    address: Optional[str] = None,
    depth: int = 2,
    max_messages: int = 60,
    max_sites: int = 10,
    max_resolve: int = 8,
    max_wallets: int = 6,
    time_budget: int = 90,
    report: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Trace a supply chain to its source: read posts, pull @handles, storefront domains and payment addresses, probe sites for their upstream, re-check handles on Telegram, follow USDT (Tron) money flows, then rank where the clues converge. Returns JSON plus a Mermaid graph.

    Args:
        query: Keywords (searched in your chats and public channels).
        chat_id: One chat or a list to read instead; with query, search inside them, without, only posts containing links.
        address: Start from a Tron wallet instead and follow where its USDT goes.
        depth: Hops to follow, 1-3.
        max_messages / max_sites / max_resolve / max_wallets: Budgets for seed posts, sites probed, @handles looked up (rate-limited), wallets queried on-chain.
        time_budget: Seconds before returning what was found so far.
        report: Also write a shareable HTML report (interactive graph + tables) into the allowed download directory.
    """
    try:
        if not query and chat_id is None and not address:
            raise ValidationError("Give query, chat_id and/or address.")
        depth = max(1, min(int(depth), 3))
        deadline = time.monotonic() + time_budget
        posts, notes = [], []
        graph, sellers, frontier = _Graph(), set(), []
        if address:
            chain = "tron" if valid_tron(address) else "eth" if _ETH_ADDR.fullmatch(address) else None
            if not chain:
                raise ValidationError("address must be a valid Tron (T...) or Ethereum (0x...) address.")
            graph.add(address, "wallet", _short(address), 0, chain=chain)
            frontier.append((address, "wallet"))
        if chat_id is not None:
            chats = chat_id if isinstance(chat_id, list) else [chat_id]
            per_chat = max(10, max_messages // len(chats))
            for cid in chats:
                try:
                    ent = await resolve_entity(cid)
                except Exception as e:
                    notes.append(f"{cid} 打不开：{type(e).__name__}")
                    continue
                label = getattr(ent, "title", None) or getattr(ent, "first_name", None) or str(cid)
                posts += await _seed_posts(ent, query, per_chat, lambda m, label=label: label)
        elif query:
            posts += await _seed_posts(None, query, max_messages, _chat_label)
            try:
                pub, status = await _public_posts(query, max_messages)
            except Exception as e:
                pub, status = [], type(e).__name__
            posts += pub
            if status:
                notes.append(f"公开频道搜索没用上：{status}")
        if not posts and not address:
            return dump_json({"summary": "没搜到帖子。换个关键词，或者指定 chat_id。", "posts_read": 0})

        for p in posts:
            sellers.add(p["sender"])
            graph.add(p["sender"], "seller", p["sender_label"], 0)
            frontier += graph.absorb(p["sender"], p["text"], 1, (p["fwd"], p["fwd_label"]))
        notes += await _crawl(graph, frontier, depth, {"sites": max_sites, "resolve": max_resolve, "wallets": max_wallets, "deadline": deadline})

        cands = _candidates(graph, sellers | ({address} if address else set()))  # 起点钱包不算汇聚点
        seller_rows = sorted(
            (
                {
                    "handle": k, "name": graph.nodes[k]["label"], "posts": sum(1 for p in posts if p["sender"] == k),
                    "sites": sorted(d for s, d, rel in graph.edges if s == k and rel == "贴链接"),
                    "mentions": sorted(d for s, d, rel in graph.edges if s == k and rel in ("提到", "转发自")),
                }
                for k in sellers
            ),
            key=lambda r: -r["posts"],
        )[:15]
        sites = {
            k: {f: n[f] for f in ("verdict", "title", "platform", "ip", "cdn", "note", "contacts", "calls", "redirects_to", "error") if n.get(f)}
            for k, n in graph.nodes.items() if n["kind"] == "site" and n.get("checked")
        }
        handles = {
            k: {f: n[f] for f in ("kind", "label", "flags", "posts", "bio", "error") if n.get(f)}
            for k, n in graph.nodes.items() if k not in sellers and n.get("checked") and n["kind"] not in ("site", "wallet")
        }
        wallets = {
            k: {f: n[f] for f in ("chain", "tx", "in_usdt", "out_usdt", "payers", "payees", "first", "last", "top_in", "top_out", "note", "explorer", "error") if n.get(f)}
            for k, n in graph.nodes.items() if n["kind"] == "wallet" and n.get("checked")
        }
        head = f"读了 {len(posts)} 条帖、{len(sellers)} 个卖家，查了 {len(sites)} 个站，核对了 {len(handles)} 个 @号，查了 {len(wallets)} 个钱包的链上流水。"
        if cands:
            c = cands[0]
            head += f" 线索汇聚点：{c['node']}（{'；'.join(c['reasons'])}）。"
        else:
            head += " 没有汇聚点：各家线索互不重叠，加大 max_messages 或换个群再试。"
        out = {
            "summary": head, "posts_read": len(posts), "sellers": seller_rows, "candidates": cands,
            "sites": sites, "handles": handles, "wallets": wallets, "truncated": notes, "mermaid": _mermaid(graph, cands, sellers),
        }
        if report:
            title = "溯源：" + (query or (address and _short(address)) or (", ".join(map(str, chat_id)) if isinstance(chat_id, list) else str(chat_id)))
            name = f"trace-{datetime.now(timezone.utc).astimezone():%Y%m%d-%H%M}-{_slug(title[3:])}.html"
            out["report"] = await _write_report(name, render_html(out, _graph_payload(graph, cands, sellers), title=title), ctx)
        return dump_json(out)
    except Exception as e:
        return log_and_format_error("trace_source", e, query=query, chat_id=chat_id)


# ---------- 工具二：卖家一眼看 ----------

_DISCLAIMER = (
    "只做相对可核查度分层，不构成购买背书。这个市场天然有封号、掉订阅、不到账、"
    "售后失联的风险，且多数商业模式违反官方条款。任何档位都不代表安全。"
)
_TIER = {
    "A": "近期活跃 + 报价明确 + 卡网可访问 + 无负面命中",
    "B": "活跃或频次高，但证据单一 / 报价不透明 / 商品风险更高",
    "C": "证据不足，或长期沉寂，或商业模式风险高",
    "D": "已消失 / 痕迹被抹除 / 卡网被判钓鱼 / 群里被指认 / 官方标记",
}
_NEAR = 25  # 负面词离 @号 多少字以内才算在说他（远处的 @ 多半无关）


async def _find_seller(handle: ChatRef):
    """按 @用户名 找卖家；查无此人返回 None —— 这是最有价值的信号，不能被当成「解析失败」吞掉。"""
    if isinstance(handle, str) and handle != "me":
        try:
            res = await client(functions.contacts.ResolveUsernameRequest(username=handle.lstrip("@")))
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
            return None
        found = list(res.users) + list(res.chats)
        return found[0] if found else None
    return await resolve_entity(handle)


def _post_stats(posts: List[Dict[str, Any]]) -> Dict[str, Any]:
    texts = [p["text"] for p in posts]
    blob = "\n".join(texts)
    dates = [p["date"] for p in posts if p.get("date")]
    last = max(dates) if dates else None
    fingerprints = {norm_text(t) for t in texts if len(norm_text(t)) >= 20}
    stale = [p for p in posts if p["edited_old"] or p["fwd_old"]]
    return {
        "n": len(posts), "last": last,
        "days": (datetime.now(timezone.utc) - last).days if last else None,
        "distinct": len(fingerprints),
        "spam_ratio": round(len(posts) / len(fingerprints), 1) if fingerprints else None,
        "priced": sum(1 for t in texts if _PRICE.search(t)),
        "prices": list(dict.fromkeys(m.group(0).strip() for m in _PRICE.finditer(blob)))[:10],
        "domains": [h for h, _ in Counter(h for t in texts for h in hosts_in(t)).most_common(10)],
        "mentions": [h for h, _ in Counter(h for t in texts for h in handles_in(t)).most_common(10)],
        "signals": {k: v for k, v in (
            ("crypto", sorted({a for _, a in wallets_in(blob)})), ("wechat", sorted({w.lower() for w in WECHAT_RE.findall(blob)})),
            ("email", sorted({e.lower() for e in EMAIL_RE.findall(blob)})),
        ) if v},
        "edited_old": sum(p["edited_old"] for p in posts), "fwd_old": sum(p["fwd_old"] for p in posts),
        "via_bot": sum(p["via_bot"] for p in posts),
        "fwd_from": [k for k, _ in Counter(p["fwd_label"] or p["fwd"] for p in posts if p["fwd"]).most_common(5)],
        "stale_examples": [
            {
                "dimension": "来路", "chat": p["chat"], "message_id": p["id"],
                "quote": (f"发于 {p['date']:%Y-%m-%d}，改于 {p['edit_date']:%Y-%m-%d}" if p["edited_old"] else f"发于 {p['date']:%Y-%m-%d}，转发的是 {p['fwd_label'] or p['fwd']} 的老帖")
                + "：" + p["text"][:80],
            }
            for p in stale[:3]
        ],
    }


async def _accusations(username: str, self_id: int, limit: int = 60) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """搜别人提到他的话：负面词挨着他的 @号 才算指认；顺带抓「他改名成 @xxx」这类马甲句式（返回 {号: 原话}）。"""
    acc, aliases = [], {}
    low = username.casefold()
    for m in await client.get_messages(None, search=username, limit=limit):
        if getattr(m, "sender_id", None) == self_id:
            continue  # 自己的广告不算
        text = m.message or ""
        for pat in ALIAS_PATTERNS:
            for a, b in pat.findall(text):
                if low in (a.casefold(), b.casefold()):
                    other = b if a.casefold() == low else a
                    aliases[other.casefold()] = f"群友说：{text[:80]}"
        at = [mm.start() for mm in re.finditer(re.escape(username), text, re.I)]
        words = [w for w in NEGATIVE_WORDS if any(abs(k.start() - p) <= _NEAR for k in re.finditer(w, text) for p in at)]
        if words:
            acc.append({"chat": _chat_label(m), "date": f"{m.date:%Y-%m-%d}", "message_id": m.id, "words": words, "quote": text[:200]})
    return acc, aliases


def _tier(flags_official: List[str], stats: Dict[str, Any], sites: Dict[str, Any], acc: List[Dict[str, Any]],
          opacity: List[str], renamed: List[str]) -> Tuple[str, List[str], List[Dict[str, Any]]]:
    """分档规则全部写死、可复算，照抄 market_watch 的报告规则，不自己发明。"""
    flags, ev, hard, soft = [], [], False, False
    for f in ("scam", "fake"):
        if f in flags_official:
            flags.append(f"官方标记 {f}")
            hard = True
    bad = [h for h, r in sites.items() if r.get("verdict") == "cloudflare_phishing"]
    if bad:
        flags.append("卡网被 Cloudflare 判定为钓鱼：" + "、".join(bad))
        hard = True
    if acc:
        flags.append(f"群里被指认 {len(acc)} 次：" + "、".join(sorted({w for a in acc for w in a['words']})))
        ev += [{"dimension": "负面", **{k: a[k] for k in ("chat", "date", "message_id", "quote")}} for a in acc[:5]]
        if len(acc) >= 2:
            hard = True
        else:
            soft = True  # 一次可能是「@他 不是骗子」这类误伤，降到 C 并把原话给人看
    n = stats["n"]
    if stats["spam_ratio"] and stats["spam_ratio"] >= 5:
        flags.append(f"活跃度靠刷屏：{n} 条只有 {stats['distinct']} 条不同文案（{stats['spam_ratio']} 倍）")
        soft = True
    stale = stats["edited_old"] + stats["fwd_old"]
    if n and stale / n >= 0.5:
        flags.append(f"{n} 条里 {stale} 条是老帖改价或转发老广告，不是新鲜现货")
        ev += stats["stale_examples"][:2]
        soft = True
    if n >= 5 and stats["priced"] == 0:
        flags.append(f"从不公开报价（{n} 条 0 条带价）")
        soft = True
    if stats["days"] is not None and stats["days"] > 60:
        flags.append(f"沉寂 {stats['days']} 天")
        soft = True
    if len(opacity) >= 2:
        flags.append("结构不透明：" + "；".join(opacity))
        soft = True
    if renamed:
        # 群友明说「他从 @a 改成了 @b」才算马甲；卡网上留的客服号是同一摊子的人，只记在 related_accounts
        flags.append("群友说他改过名 / 有小号：" + "、".join(f"@{h}" for h in renamed))
        soft = True
    if hard:
        tier = "D"
    elif soft:
        tier = "C"
    elif n and stats["days"] is not None and stats["days"] <= 14 and stats["priced"]:
        tier = "A"
    else:
        tier = "B"
    return tier, flags, ev


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Check Seller", openWorldHint=True, readOnlyHint=True))
@validate_id("handle", "source_chats")
async def check_seller(
    handle: ChatRef, source_chats: Optional[List[ChatRef]] = None, sample: int = 50, probe_sites: bool = True,
    report: bool = False, ctx: Optional[Context] = None,
) -> str:
    """One-glance trust card for a seller: alive, official flags, posting history, provenance (old posts re-edited, stale ads forwarded), spam ratio, storefront phishing check, accusations next to their @handle, renamed/alt accounts and related accounts — then a tier A-D with quoted evidence.

    Args:
        handle: @username or ID of the seller (user, bot or channel).
        source_chats: Groups where their ads appear; default = groups you share with them.
        sample: How many of their posts to read.
        probe_sites: Fetch their storefront domains (HTTP) to detect Cloudflare phishing flags.
        report: Also write a shareable HTML card into the allowed download directory.
    """
    try:
        today = f"{datetime.now(timezone.utc):%Y-%m-%d}"
        ent = await _find_seller(handle)
        if ent is None:
            return dump_json({
                "handle": str(handle), "checked_at": today, "tier": "D", "tier_definition": _TIER["D"],
                "red_flags": ["用户名已不存在（跑路、改名或被封）"], "evidence": [], "confidence": "high", "disclaimer": _DISCLAIMER,
            })
        key, label, kind = _entity_key(ent)
        username = getattr(ent, "username", None)
        profile: Dict[str, Any] = {"id": ent.id, "name": label, "kind": kind, "username": username}
        official = [f for f in ("scam", "fake", "restricted", "verified", "premium") if getattr(ent, f, False)]
        if official:
            profile["flags"] = official
        opacity, common = [], []
        if kind in ("user", "bot"):
            try:
                fu = (await client(functions.users.GetFullUserRequest(id=ent))).full_user
                if fu.about:
                    profile["bio"] = fu.about
                profile["common_chats"] = fu.common_chats_count
                if fu.common_chats_count and not source_chats:
                    common = list((await client(functions.messages.GetCommonChatsRequest(user_id=ent, max_id=0, limit=20))).chats)
            except Exception as e:
                logger.warning(f"full user failed for {handle}: {e}")
        else:
            try:
                fc = (await client(functions.channels.GetFullChannelRequest(channel=ent))).full_chat
                if fc.about:
                    profile["bio"] = fc.about
                profile["members"] = fc.participants_count
                if getattr(ent, "noforwards", False):
                    opacity.append("禁止转发/截图外传")
                if getattr(fc, "hidden_prehistory", False):
                    opacity.append("新成员看不到入群前历史")
                if kind == "channel" and not getattr(fc, "linked_chat_id", None):
                    opacity.append("没有评论区")
            except Exception as e:
                logger.warning(f"full channel failed for {handle}: {e}")

        # 个人卖家的广告在群里，不在「我和他的私聊」里；频道的帖就在频道里
        posts, sampled_from = [], []
        if kind in ("user", "bot"):
            chats = [await resolve_entity(c) for c in source_chats] if source_chats else common
            for c in chats:
                try:
                    got = await client.get_messages(c, limit=sample, from_user=ent)
                except Exception:
                    continue
                title = getattr(c, "title", None) or str(getattr(c, "id", "?"))
                sampled_from.append(title)
                posts += [_post(m, title) for m in got]
                if len(posts) >= sample:
                    break
        else:
            posts = [_post(m, label) for m in await client.get_messages(ent, limit=sample)]
            sampled_from = [label]
        stats = _post_stats(posts)

        sites: Dict[str, Any] = {}
        if probe_sites and stats["domains"]:
            for r in await asyncio.gather(*(probe_site(h) for h in stats["domains"][:5])):
                sites[r["host"]] = {k: v for k, v in r.items() if k != "host" and v}

        # 钱的情况：他贴过的波场收款地址，链上进出账和钱主要转去了哪
        money: Dict[str, Any] = {}
        for addr in [a for a in stats["signals"].get("crypto", []) if valid_tron(a)][:3]:
            try:
                money[addr] = summarize_transfers(addr, await _tron_transfers(addr))
            except Exception as e:
                money[addr] = {"error": str(e)}

        acc, related = await _accusations(username, ent.id) if username else ([], {})
        renamed = sorted(related)
        low = (username or "").casefold()
        for host, r in sites.items():
            for c in r.get("contacts", []):
                if c != low:
                    related.setdefault(c, f"卡网 {host} 上留的联系方式")
        for m in stats["mentions"]:
            if low and name_kin(m, low):
                related.setdefault(m, "名字同源")

        tier, flags, ev = _tier(official, stats, sites, acc, opacity, renamed)
        upstream = [f"转发自 {x}" for x in stats["fwd_from"]] + [f"{h} 用的是 {r['platform']}" for h, r in sites.items() if r.get("platform")]
        upstream += [
            f"收款地址 {_short(a)} 的钱主要转去 {_short(m['top_out'][0]['address'])}（{_usdt(m['top_out'][0]['usdt'])}）"
            for a, m in money.items() if m.get("top_out")
        ]
        if not posts:
            confidence = "low"
            flags.append("没找到他的帖子：个人卖家的广告发在群里，传 source_chats 再查" if kind in ("user", "bot") else "读不到帖子")
        else:
            confidence = "high" if ev else "medium"
        card = {
            "handle": f"@{username}" if username else str(handle), "checked_at": today,
            "tier": tier, "tier_definition": _TIER[tier], "red_flags": flags, "evidence": ev,
            "profile": profile,
            "activity": {
                "posts_sampled": stats["n"], "sampled_from": sampled_from, "last_post": stats["last"],
                "days_since_last": stats["days"], "distinct_texts": stats["distinct"], "spam_ratio": stats["spam_ratio"],
                "posts_with_price": stats["priced"],
            },
            "provenance": {"edited_old": stats["edited_old"], "forwarded_old": stats["fwd_old"], "via_bot": stats["via_bot"], "forwarded_from": stats["fwd_from"]},
            "clues": {"domains": stats["domains"], "mentions": stats["mentions"], "prices": stats["prices"], "signals": stats["signals"]},
            "sites": sites, "money": money, "accusations": acc[:10],
            "related_accounts": [{"handle": f"@{h}", "why": why} for h, why in related.items()],
            "upstream_hints": upstream, "confidence": confidence, "disclaimer": _DISCLAIMER,
        }
        if report:
            name = f"seller-{_slug(card['handle'])}-{datetime.now(timezone.utc).astimezone():%Y%m%d-%H%M}.html"
            card["report"] = await _write_report(name, render_html({"summary": f"{card['handle']}：{tier} 档 —— {_TIER[tier]}"}, None, [card], title=f"卖家一眼看：{card['handle']}"), ctx)
        return dump_json(card)
    except Exception as e:
        return log_and_format_error("check_seller", e, handle=handle)
