"""市场雷达：盯一批卖家和溯源任务，重跑时只查上次点过名的对象，跟上次快照比，有变化就一行一条推出去。

为什么只盯「点过名的」而不是全网重爬：2026-04-29 那次 depth=5 / 5 万条消息的全遍历触发过 18 小时限速；
真正要回答的问题只有「上次这十几个对象，现在怎么样了」，几十秒就够。
快照是窄的、自己定义的、只此一份，所以天然可比；每次结果带日期追加，不覆盖 —— 跨时间比对是全部价值。

通知：`RADAR_NOTIFY_CMD` 是一条 shell 命令，变化文本从标准输入进去（飞书 / Slack / 任何推送命令都行）；
定时：`RADAR_EVERY_HOURS` 设了就每隔这么久自动跑一轮（常驻进程里的后台任务，重启不重置计时）。
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from . import app
from .app import logger, mcp
from .common import ChatRef, ValidationError, dump_json, log_and_format_error, require, validate_id, writable_path
from .trace import _PAUSE, _short, check_seller, trace_source

STORE_NAME = "radar.json"
_HISTORY_CAP = 60  # 每个对象最多留多少份快照；一份约 1KB
_DORMANT_DAYS = 60
_TRACE_ARGS = {"depth": 2, "max_messages": 80, "time_budget": 120}  # ponytail: 溯源任务的预算写死，要调就改这里
_FLOOD_MARKS = ("Rate limited", "FloodWait", "flood")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


# ---------- 快照：把卡片 / 溯源结果压成窄的一行 ----------


def snap_seller(card: Dict[str, Any]) -> Dict[str, Any]:
    prof, act, money = card.get("profile") or {}, card.get("activity") or {}, card.get("money") or {}
    return {
        "at": _now(), "alive": bool(prof.get("id")), "id": prof.get("id"), "username": prof.get("username"), "name": prof.get("name"),
        "flags": sorted(prof.get("flags") or []), "tier": card.get("tier"), "red_flags": list(card.get("red_flags") or []),
        "last_post": (act.get("last_post") or "")[:10] or None, "days": act.get("days_since_last"),
        "domains": sorted((card.get("clues") or {}).get("domains") or []), "prices": list((card.get("clues") or {}).get("prices") or []),
        "accusations": len(card.get("accusations") or []), "wallets": sorted(((card.get("clues") or {}).get("signals") or {}).get("crypto") or []),
        "top_out": {a: m["top_out"][0]["address"] for a, m in money.items() if m.get("top_out")},
        "sites": {h: r.get("verdict") for h, r in (card.get("sites") or {}).items() if r.get("verdict")},
    }


def snap_trace(res: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "at": _now(), "posts": res.get("posts_read", 0), "sellers": sorted(s["handle"] for s in res.get("sellers") or []),
        "sites": sorted(res.get("sites") or {}), "candidates": [c["node"] for c in res.get("candidates") or []],
        "wallets": sorted(res.get("wallets") or {}), "handles": sorted(res.get("handles") or {}),
    }


def _pm(label: str, old: List[str], new: List[str], cap: int = 4) -> List[str]:
    a, b = set(old or []), set(new or [])
    out = []
    if b - a:
        out.append(f"{label}多了 {len(b - a)} 个：{' '.join(sorted(b - a)[:cap])}")
    if a - b:
        out.append(f"{label}少了 {len(a - b)} 个：{' '.join(sorted(a - b)[:cap])}")
    return out


def diff_seller(o: Dict[str, Any], n: Dict[str, Any]) -> List[str]:
    ch: List[str] = []
    if o.get("alive") and not n.get("alive"):
        return ["跑路了：用户名已不存在"]
    if not o.get("alive") and n.get("alive"):
        ch.append("又出现了")
    if o.get("id") and n.get("id") and o["id"] != n["id"]:
        ch.append(f"用户名换了主人（ID {o['id']} → {n['id']}）")
    if o.get("tier") != n.get("tier") and n.get("tier"):
        ch.append(f"{o.get('tier') or '?'} 档 → {n['tier']} 档")
    for f in sorted(set(n.get("flags") or []) - set(o.get("flags") or [])):
        ch.append(f"被官方标记 {f}")
    if o.get("name") != n.get("name") and n.get("name"):
        ch.append(f"改名：{o.get('name')!r} → {n['name']!r}")
    new_flags = [f for f in n.get("red_flags") or [] if f not in (o.get("red_flags") or [])]
    if new_flags:
        ch.append("新红旗：" + "；".join(f[:40] for f in new_flags[:3]))
    if (n.get("accusations") or 0) > (o.get("accusations") or 0):
        ch.append(f"新增 {n['accusations'] - (o.get('accusations') or 0)} 条指认（共 {n['accusations']}）")
    ch += _pm("卡网", o.get("domains"), n.get("domains")) + _pm("报价", o.get("prices"), n.get("prices"))
    od, nd = o.get("days"), n.get("days")
    if od is not None and nd is not None:
        if od <= _DORMANT_DAYS < nd:
            ch.append(f"沉寂超过 {_DORMANT_DAYS} 天")
        elif od > _DORMANT_DAYS and nd <= 7:
            ch.append("又开始发帖了")
    for a, dst in (n.get("top_out") or {}).items():
        was = (o.get("top_out") or {}).get(a)
        if was and was != dst:
            ch.append(f"收款地址 {_short(a)} 的钱改转去 {_short(dst)}")
    for h, v in (n.get("sites") or {}).items():
        was = (o.get("sites") or {}).get(h)
        # ponytail: 5xx 多半是一次探测撞上的抖动（实测第一轮就撞到一次 503），不报；要报「挂了」就改成连续两次再报
        if was and was != v and not any(str(s).startswith("http_5") for s in (was, v)):
            ch.append(f"卡网 {h}：{was} → {v}")
    return ch


def diff_trace(o: Dict[str, Any], n: Dict[str, Any]) -> List[str]:
    ch = _pm("卖家", o.get("sellers"), n.get("sellers"))
    oc, nc = (o.get("candidates") or [None])[0], (n.get("candidates") or [None])[0]
    if oc != nc:
        ch.append(f"汇聚点换了：{oc} → {nc}" if oc and nc else f"出现汇聚点：{nc}" if nc else f"汇聚点没了（原来是 {oc}）")
    ch += _pm("站", o.get("sites"), n.get("sites")) + _pm("收款地址", o.get("wallets"), n.get("wallets"), cap=2)
    return ch


def brief(w: Dict[str, Any]) -> Optional[str]:
    """一个对象最近一份快照的一句话。"""
    h = w.get("history") or []
    if not h:
        return None
    s = h[-1]
    if w["kind"] == "seller":
        return ("已消失" if not s.get("alive") else f"{s.get('tier')} 档") + (f"，{s['accusations']} 条指认" if s.get("accusations") else "") + (f"，{s['days']} 天没发帖" if s.get("days") is not None else "")
    return f"{s.get('posts')} 帖 / {len(s.get('sellers') or [])} 卖家 / {len(s.get('sites') or [])} 站" + (f"，汇聚点 {s['candidates'][0]}" if s.get("candidates") else "")


def render_text(at: str, total: int, changed: List[Dict[str, Any]], errors: List[Dict[str, Any]]) -> str:
    head = f"📡 市场雷达 {at} · 盯 {total} 个 · " + (f"{len(changed)} 个有变化" if changed else "没变化")
    lines = [head] + [f"• {c['name']}：{'；'.join(c['changes'])}" for c in changed]
    if errors:
        lines.append(f"• 查不了 {len(errors)} 个：" + "；".join(f"{e['name']}（{e['error'][:60]}）" for e in errors[:3]))
    return "\n".join(lines)


# ---------- 存取 ----------


async def _store_path(file: Optional[str], ctx: Optional[Context]) -> Any:
    path, err = await writable_path(file, STORE_NAME, ctx)
    if err:
        raise ValidationError(err)
    return path


def load(path) -> Dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"watches": {}, "runs": []}


def save(path, store: Dict[str, Any]) -> None:
    for w in store["watches"].values():
        w["history"] = w.get("history", [])[-_HISTORY_CAP:]
    store["runs"] = store.get("runs", [])[-100:]
    path.write_text(json.dumps(store, ensure_ascii=False, indent=0), encoding="utf-8")


# ---------- 跑 ----------


async def _check(w: Dict[str, Any], ctx: Optional[Context]) -> Dict[str, Any]:
    if w["kind"] == "seller":
        return snap_seller(json.loads(await check_seller(handle=w["handle"], source_chats=w.get("source_chats"), ctx=ctx)))
    return snap_trace(json.loads(await trace_source(**w["args"], **_TRACE_ARGS, ctx=ctx)))


async def notify(text: str) -> Tuple[bool, str]:
    """把文本喂给 RADAR_NOTIFY_CMD 的标准输入。没配就不发，调用方自己转发。"""
    cmd = os.getenv("RADAR_NOTIFY_CMD")
    if not cmd:
        return False, "没配 RADAR_NOTIFY_CMD，没推送；把 text 自己转发出去"
    try:
        proc = await asyncio.create_subprocess_shell(cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(text.encode()), timeout=60)
        if proc.returncode == 0:
            return True, "已推送"
        return False, f"推送命令退出码 {proc.returncode}：{out.decode(errors='replace')[-200:]}"
    except Exception as e:
        return False, f"推送命令失败：{type(e).__name__}: {e}"[:200]


async def run(store: Dict[str, Any], names: Optional[List[str]], ctx: Optional[Context], send: bool) -> Dict[str, Any]:
    at, changed, unchanged, errors, skipped = _now(), [], [], [], []
    todo = [n for n in (names or list(store["watches"])) if n in store["watches"]]
    for i, name in enumerate(todo):
        w = store["watches"][name]
        if skipped or (errors and any(m in errors[-1]["error"] for m in _FLOOD_MARKS)):
            skipped.append(name)  # 已经撞限速了，剩下的别再撞
            continue
        if i:
            await asyncio.sleep(_PAUSE * 2)
        try:
            cur = await _check(w, ctx)
        except Exception as e:
            errors.append({"name": name, "error": f"{type(e).__name__}: {e}"[:200]})
            continue  # 查失败的不进历史，下次还跟上次成功的那份比
        last = (w.get("history") or [None])[-1]
        if last:
            ch = (diff_seller if w["kind"] == "seller" else diff_trace)(last, cur)
            if ch:
                changed.append({"name": name, "changes": ch, "since": last["at"]})
            else:
                unchanged.append(name)
        w.setdefault("history", []).append(cur)
    text = render_text(at, len(todo), changed, errors + [{"name": n, "error": "限速后跳过"} for n in skipped])
    notified, note = (await notify(text)) if send and (changed or errors) else (False, "没变化，不推送" if send else "notify=False")
    store.setdefault("runs", []).append({"at": at, "checked": len(todo) - len(skipped), "changed": len(changed), "errors": len(errors) + len(skipped), "notified": notified})
    return {"at": at, "checked": len(todo) - len(skipped), "changed": changed, "unchanged": unchanged, "errors": errors, "skipped": skipped, "text": text, "notified": notified, "notify_note": note}


# ---------- 定时：常驻进程里每隔 RADAR_EVERY_HOURS 小时跑一轮 ----------


def due(store: Dict[str, Any], hours: float, now: Optional[datetime] = None) -> bool:
    """按文件里记的上次时间算，进程重启不重置计时。"""
    if not store.get("watches"):
        return False
    runs = store.get("runs") or []
    if not runs:
        return True
    last = datetime.strptime(runs[-1]["at"], "%Y-%m-%d %H:%M").replace(tzinfo=datetime.now(timezone.utc).astimezone().tzinfo)
    return ((now or datetime.now(timezone.utc).astimezone()) - last).total_seconds() >= hours * 3600


async def _timer() -> None:
    hours = float(os.getenv("RADAR_EVERY_HOURS", "0") or 0)
    if hours <= 0:
        return
    logger.warning(f"市场雷达：每 {hours:g} 小时自动跑一轮")
    while True:
        await asyncio.sleep(900)
        try:
            try:
                path = await _store_path(None, None)
            except ValidationError as e:
                logger.error(f"市场雷达定时器停了：{e}")  # 没给允许的目录就没地方存快照
                return
            store = load(path)
            if due(store, hours):
                res = await run(store, None, None, send=True)
                save(path, store)
                logger.warning(f"市场雷达自动跑完：{res['text'].splitlines()[0]}；{res['notify_note']}")
        except Exception as e:
            logger.error(f"市场雷达自动跑失败：{type(e).__name__}: {e}")


app.STARTUP_TASKS.append(_timer)


# ---------- 工具 ----------


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Market Radar", openWorldHint=True, readOnlyHint=False, destructiveHint=False, idempotentHint=False))
@validate_id("handle", "source_chats", "chat_id")
async def market_radar(
    action: str,
    handle: Optional[ChatRef] = None,
    source_chats: Optional[List[ChatRef]] = None,
    query: Optional[str] = None,
    chat_id: Optional[Union[ChatRef, List[ChatRef]]] = None,
    name: Optional[str] = None,
    notify: bool = True,
    file: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> str:
    """Market radar: watch sellers and traces over time. Each run re-checks only what you listed, diffs against the last snapshot (gone, tier change, renamed, official flag, new accusations, storefront/price changes, new sellers, convergence point moved, money re-routed) and pushes a one-line-per-change digest.

    Args:
        action: add (seller by handle, or trace by query/chat_id; also takes the first snapshot) / run (all or one name) / list / history / remove.
        handle: For add: seller @username or ID. source_chats: groups where their ads appear.
        query / chat_id: For add: a trace to watch (same meaning as trace_source).
        name: Watch name (default @handle or the query); for run/history/remove.
        notify: For run: push the digest through RADAR_NOTIFY_CMD (shell command, text on stdin) when something changed.
        file: Store path; default <allowed dir>/downloads/radar.json. RADAR_EVERY_HOURS makes the daemon run it on its own.
    """
    try:
        path = await _store_path(file, ctx)
        store = load(path)
        watches = store["watches"]
        if action == "add":
            if handle is not None:
                key = name or (f"@{handle.lstrip('@')}" if isinstance(handle, str) else str(handle))
                w = {"kind": "seller", "handle": handle, "source_chats": source_chats, "added": _now(), "history": []}
            elif query or chat_id is not None:
                key = name or query or (", ".join(map(str, chat_id)) if isinstance(chat_id, list) else str(chat_id))
                w = {"kind": "trace", "args": {k: v for k, v in (("query", query), ("chat_id", chat_id)) if v is not None}, "added": _now(), "history": []}
            else:
                raise ValidationError("Give handle (a seller) or query / chat_id (a trace) to watch.")
            if key in watches:
                raise ValidationError(f"Already watching {key!r}; remove it first or pick another name.")
            cur = await _check(w, ctx)
            w["history"].append(cur)
            watches[key] = w
            save(path, store)
            return dump_json({"status": "added", "name": key, "kind": w["kind"], "baseline": cur, "watching": len(watches), "store": str(path)})
        if action == "run":
            if name and name not in watches:
                raise ValidationError(f"No watch named {name!r}. Use list.")
            if not watches:
                raise ValidationError("Nothing to watch yet. Use add first.")
            res = await run(store, [name] if name else None, ctx, send=notify)
            save(path, store)
            return dump_json(res)
        if action == "list":
            return dump_json({
                "store": str(path), "watching": len(watches), "last_run": (store.get("runs") or [{}])[-1].get("at"),
                "watches": [{"name": k, "kind": w["kind"], "added": w["added"], "snapshots": len(w.get("history") or []), "last": brief(w)} for k, w in watches.items()],
            })
        if action == "history":
            require(action, name=name)
            if name not in watches:
                raise ValidationError(f"No watch named {name!r}.")
            return dump_json({"name": name, "kind": watches[name]["kind"], "history": watches[name].get("history") or []})
        if action == "remove":
            require(action, name=name)
            if watches.pop(name, None) is None:
                raise ValidationError(f"No watch named {name!r}.")
            save(path, store)
            return dump_json({"status": "removed", "name": name, "watching": len(watches)})
        raise ValidationError("Unknown action. Use add / run / list / history / remove.")
    except Exception as e:
        return log_and_format_error("market_radar", e, action=action, name=name, handle=handle)
