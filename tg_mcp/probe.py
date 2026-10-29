"""AI 替你套话：以买家口吻私聊卖家，问价、问付款方式、问能不能批发、问是不是源头；答复自动记下来、抽出线索。

一次调用不阻塞等回复：start 发第一句，之后每次 check 读有没有新答复，有就追下一问；report 看全程记录和抽出的线索。
用哪个号由用户自己定（主号还是小号都行，工具不拦）。但要知道：被限的号仍能回复先发消息的人，所以风险集中在
给陌生人的**第一句** —— 默认每天最多开 5 个新对话、两次开场隔 10 分钟、同一个人 7 天内不重复问、发前有「正在输入」
的停顿；这些都是默认值，环境变量能改，force=True 能跳过。
"""

import asyncio
import json
import os
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from .app import client, logger, mcp
from .common import WECHAT_RE, ChatRef, ValidationError, dump_json, log_and_format_error, require, validate_id, writable_path
from . import prices
from .trace import _PRICE, _entity_key, _find_seller, handles_in, hosts_in, wallets_in

STORE_NAME = "probes.json"
DAILY_CAP = int(os.getenv("TG_MCP_PROBE_DAILY_CAP", "5"))  # 每天最多给几个陌生人开场
MIN_GAP_MIN = int(os.getenv("TG_MCP_PROBE_GAP_MIN", "10"))  # 两次开场至少隔几分钟
COOLDOWN_DAYS = int(os.getenv("TG_MCP_PROBE_COOLDOWN_DAYS", "7"))  # 同一个人多少天内不再问
GIVE_UP_HOURS = 48  # 这么久不回就算没回
_PAUSE_RANGE = (3.0, 8.0)  # 发前「正在输入」停顿的秒数区间
DEFAULT_SCRIPT = [
    "你好，在吗？{product} 现在什么价？",
    "怎么付款？走卡网还是直接转 U？",
    "量大有优惠吗？拿货价多少、多少起批？",
    "你这是一手的还是代理的？上家是哪家，稳定吗？",
]
_SOURCE = re.compile(r"一手|源头|自己做|自己的号|官方直|厂家|我们自己|自营")
_AGENT = re.compile(r"代理|拿货|上家|上游|供应商|批发给我|我也是拿")
_PAY = [("卡网", re.compile(r"卡网|自助|下单链接|发卡")), ("USDT", re.compile(r"\bU\b|usdt|泰达|链上|trc", re.I)), ("支付宝/微信", re.compile(r"支付宝|微信|wx|vx|zfb", re.I)), ("担保", re.compile(r"担保|中介|押金"))]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone().strftime("%Y-%m-%d %H:%M")


def clues_from(texts: List[str]) -> Dict[str, Any]:
    blob = "\n".join(texts)
    out: Dict[str, Any] = {}
    quotes = [{"item": q["category"], "cny": q["cny"], "raw": q["raw"]} for q in prices.extract(blob)]
    raw = list(dict.fromkeys(m.group(0).strip() for m in _PRICE.finditer(blob)))
    if quotes:
        out["quotes"] = quotes
    elif raw:
        out["prices"] = raw[:8]
    for k, v in (("domains", sorted(hosts_in(blob))), ("handles", [f"@{h}" for h in sorted(handles_in(blob))]),
                 ("wallets", sorted(a for _, a in wallets_in(blob))), ("wechat", sorted({w.lower() for w in WECHAT_RE.findall(blob)}))):
        if v:
            out[k] = v
    pay = [name for name, rx in _PAY if rx.search(blob)]
    if pay:
        out["payment"] = pay
    if _AGENT.search(blob):
        out["role"] = "自称代理 / 拿货的"
    elif _SOURCE.search(blob):
        out["role"] = "自称一手 / 源头"
    return out


def summary_of(p: Dict[str, Any]) -> str:
    c = p.get("clues") or {}
    asked = sum(1 for t in p["transcript"] if t["from"] == "me")
    answered = sum(1 for t in p["transcript"] if t["from"] == "them")
    bits = []
    if c.get("quotes"):
        bits.append("报价 " + "、".join(f"{q['item']} {q['raw']}" for q in c["quotes"][:4]))
    elif c.get("prices"):
        bits.append("报价 " + "、".join(c["prices"][:4]))
    if c.get("payment"):
        bits.append("付款走 " + " / ".join(c["payment"]))
    if c.get("domains"):
        bits.append("卡网 " + "、".join(c["domains"][:3]))
    if c.get("role"):
        bits.append(c["role"])
    if c.get("handles"):
        bits.append("提到 " + " ".join(c["handles"][:3]))
    if c.get("wallets"):
        bits.append(f"给了 {len(c['wallets'])} 个收款地址")
    status = {"waiting": "等回复", "done": "问完了", "no_reply": "没回", "stopped": "已停"}[p["status"]]
    return f"{p['handle']}：问了 {asked} 句、答了 {answered} 句，{status}" + ("；" + "；".join(bits) if bits else "；还没套出东西") + "。"


def load(path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {"probes": {}, "starts": []}
    except Exception:
        return {"probes": {}, "starts": []}


def save(path, store: Dict[str, Any]) -> None:
    store["starts"] = store.get("starts", [])[-200:]
    path.write_text(json.dumps(store, ensure_ascii=False, indent=0), encoding="utf-8")


def find(store: Dict[str, Any], user_id: int) -> Optional[Dict[str, Any]]:
    return next((p for p in store.get("probes", {}).values() if p.get("id") == user_id), None)


async def _say(entity, text: str) -> int:
    """发一句，发前假装打字停一会儿。返回消息号。"""
    secs = random.uniform(*_PAUSE_RANGE)
    try:
        async with client.action(entity, "typing"):
            await asyncio.sleep(secs)
    except Exception:
        await asyncio.sleep(secs)
    sent = await client.send_message(entity, text)
    return sent.id


async def _entity_of(p: Dict[str, Any]):
    try:
        return await client.get_entity(p["id"])  # 聊过的人在缓存里，不用再解析用户名
    except Exception:
        return await _find_seller(p["handle"])


def _guard(store: Dict[str, Any], user_id: int) -> Optional[str]:
    """开场前的三道闸：同一个人冷却期内、今天开场够多了、离上一次开场太近。"""
    old = find(store, user_id)
    if old and (_now() - datetime.fromisoformat(old["started_iso"])).days < COOLDOWN_DAYS:
        return f"{old['handle']} {COOLDOWN_DAYS} 天内已经问过（{old['started']}，{old['status']}），用 report 看记录；非要再问加 force=True。"
    starts = [datetime.fromisoformat(s) for s in store.get("starts", [])]
    today = [s for s in starts if _now() - s < timedelta(days=1)]
    if len(today) >= DAILY_CAP:
        return f"今天已经给 {len(today)} 个陌生人开过场（上限 {DAILY_CAP}，环境变量 TG_MCP_PROBE_DAILY_CAP 可改）；给陌生人的第一句是封号风险最高的动作。非要继续加 force=True。"
    if starts and _now() - max(starts) < timedelta(minutes=MIN_GAP_MIN):
        return f"离上一次开场不到 {MIN_GAP_MIN} 分钟，缓一缓；非要继续加 force=True。"
    return None


async def _lookup(probes: Dict[str, Any], handle: ChatRef) -> Optional[Dict[str, Any]]:
    """按 @号 或数字 ID 找记录；都对不上（比如传的是 me 或改过名）就解析一次拿 ID 再找。"""
    k = f"@{str(handle).lstrip('@').casefold()}"
    hit = next((p for p in probes.values() if p["handle"] == k or str(p["id"]) == str(handle)), None)
    if hit is None:
        try:
            ent = await _find_seller(handle)
            hit = find({"probes": probes}, ent.id) if ent is not None else None
        except Exception:
            hit = None
    return hit


async def _advance(p: Dict[str, Any], entity) -> Dict[str, Any]:
    """读新答复；有答复且还有问题就追下一问；太久不回就放弃。"""
    new = []
    last = p.get("last_seen_id", 0)
    for m in reversed(await client.get_messages(entity, limit=30)):
        if m.id > last and not getattr(m, "out", False) and (m.message or ""):
            new.append(m)
    asked_next = None
    if new:
        p["last_seen_id"] = max(m.id for m in new)
        for m in new:
            p["transcript"].append({"at": _iso(m.date) if m.date else None, "from": "them", "text": m.message})
        p["clues"] = clues_from([t["text"] for t in p["transcript"] if t["from"] == "them"])
        p["last_reply"] = _iso(_now())
        if p["step"] < len(p["questions"]):
            q = p["questions"][p["step"]]
            p["last_seen_id"] = max(p["last_seen_id"], await _say(entity, q))
            p["transcript"].append({"at": _iso(_now()), "from": "me", "text": q})
            p["step"] += 1
            asked_next = q
        else:
            p["status"] = "done"
    elif p["status"] == "waiting" and _now() - datetime.fromisoformat(p["started_iso"]) > timedelta(hours=GIVE_UP_HOURS):
        p["status"] = "no_reply"
    return {"handle": p["handle"], "status": p["status"], "new_replies": [m.message[:200] for m in new], "asked_next": asked_next, "clues": p.get("clues", {})}


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Probe Seller", openWorldHint=True, readOnlyHint=False, destructiveHint=False, idempotentHint=False))
@validate_id("handle")
async def probe_seller(
    action: str,
    handle: Optional[ChatRef] = None,
    questions: Optional[List[str]] = None,
    product: str = "GPT Plus",
    force: bool = False,
    file: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> str:
    """Chat a seller up as a buyer, step by step, and keep what they say. start sends the first question (no waiting); check reads new replies for all open probes, extracts clues (quotes, storefronts, payment method, wallets, "I'm a reseller / I'm the source", upstream handles) and asks the next question; report shows the transcript and clues; stop / list. Which account to use is your call; the first message to a stranger is where spam limits bite, so by default: 5 new conversations a day, 10 minutes apart, one probe per person per 7 days, a typing pause before each line.

    Args:
        action: start / check / report / stop / list.
        handle: The seller (@username or ID). start / report / stop need it; check without it advances every open probe.
        questions: Your own buyer-style questions in order (default: price → payment → bulk price → source or reseller).
        product: Fills {product} in the default questions.
        force: Skip the daily cap, the gap and the 7-day cooldown.
        file: State file; default <allowed dir>/downloads/probes.json.
    """
    try:
        path, err = await writable_path(file, STORE_NAME, ctx)
        if err:
            raise ValidationError(err)
        store = load(path)
        probes = store["probes"]
        if action == "start":
            require(action, handle=handle)
            ent = await _find_seller(handle)
            if ent is None:
                raise ValidationError(f"{handle} 用户名已不存在。")
            key, label, kind = _entity_key(ent)
            if kind not in ("user", "bot"):
                raise ValidationError("频道 / 群不能私聊，找简介里的客服号。")
            if not force:
                why = _guard(store, ent.id)
                if why:
                    raise ValidationError(why)
            qs = [q.replace("{product}", product) for q in (questions or DEFAULT_SCRIPT)]
            if not qs:
                raise ValidationError("questions is empty.")
            sent_id = await _say(ent, qs[0])
            now = _now()
            probes[key] = {
                "handle": key, "id": ent.id, "name": label, "kind": kind, "started": _iso(now), "started_iso": now.isoformat(), "questions": qs, "step": 1,
                "last_seen_id": sent_id, "transcript": [{"at": _iso(now), "from": "me", "text": qs[0]}], "clues": {}, "status": "waiting",
            }
            store.setdefault("starts", []).append(now.isoformat())
            save(path, store)
            return dump_json({"status": "started", "handle": key, "asked": qs[0], "remaining": len(qs) - 1, "next": "过一会儿用 action=check 看他回了没；回了会自动追下一问。", "store": str(path)})
        if action == "check":
            targets = [p for p in probes.values() if p["status"] == "waiting"]
            if handle is not None:
                hit = await _lookup(probes, handle)
                if hit is None:
                    raise ValidationError(f"没有 {handle} 的套话记录。")
                targets = [hit]
            results, errors = [], []
            for p in targets:
                try:
                    results.append(await _advance(p, await _entity_of(p)))
                except Exception as e:
                    errors.append({"handle": p["handle"], "error": f"{type(e).__name__}: {e}"[:160]})
            save(path, store)
            replied = [r for r in results if r["new_replies"]]
            head = f"看了 {len(results)} 个对话，{len(replied)} 个有新答复" + ("：" + "；".join(summary_of(probes[r["handle"]]) for r in replied) if replied else "。")
            return dump_json({"summary": head, "probes": results, "errors": errors})
        if action == "report":
            require(action, handle=handle)
            p = await _lookup(probes, handle)
            if p is None:
                raise ValidationError(f"没有 {handle} 的套话记录。")
            return dump_json({"summary": summary_of(p), **{k2: v for k2, v in p.items() if k2 not in ("started_iso", "last_seen_id")}})
        if action == "stop":
            require(action, handle=handle)
            p = await _lookup(probes, handle)
            if p is None:
                raise ValidationError(f"没有 {handle} 的套话记录。")
            p["status"] = "stopped"
            save(path, store)
            return dump_json({"status": "stopped", "handle": p["handle"], "summary": summary_of(p)})
        if action == "list":
            today = sum(1 for s in store.get("starts", []) if _now() - datetime.fromisoformat(s) < timedelta(days=1))
            return dump_json({"store": str(path), "today_started": today, "daily_cap": DAILY_CAP, "probes": [{"handle": p["handle"], "status": p["status"], "started": p["started"], "step": f"{p['step']}/{len(p['questions'])}", "summary": summary_of(p)} for p in probes.values()]})
        raise ValidationError("Unknown action. Use start / check / report / stop / list.")
    except Exception as e:
        return log_and_format_error("probe_seller", e, action=action, handle=handle)
