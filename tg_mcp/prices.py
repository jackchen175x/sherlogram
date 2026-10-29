"""价格行情：从帖子里抽「品类 + 报价」，按品类算中位数、离散度和「低到不可能」的红线；卖家的报价拿来跟行情比。

用中位数 + MAD 而不是均值 / 标准差：这个市场是长尾，几条离谱高价就能把均值拉飞。
「报价突然低于群内常见价太多」是识别假货最有效的单一规则，但它需要同品类基线 —— 行情表就是这条基线。
很多卖家故意不在群里报价（「详情卡网」「私聊」），那不是数据缺失，是该扣分的地方，不替他把价挖出来。
品类配对的坑：一律优先取价格**前面**的品类 —— 报价单永远是「品类 价格」，按「最近」配会把 278 配给后面的 Claude 20x。
"""

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, List, Optional, Union

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from .app import client, mcp
from .common import ChatRef, ValidationError, dump_json, log_and_format_error, resolve_entity, validate_id, writable_path

CATEGORIES = {
    "GPT Plus": [r"gpt\s*plus", r"plus\s*月卡", r"chatgpt\s*plus"],
    "GPT Pro 5x": [r"(?:gpt\s*)?pro\s*5\s*x", r"5\s*倍\s*pro"],
    "GPT Pro 20x": [r"(?:gpt\s*)?pro\s*20\s*x", r"20\s*倍\s*pro"],
    "GPT Pro": [r"(?:gpt|chatgpt)\s*pro\b(?!\s*\d+\s*x)"],
    "GPT Team": [r"gpt\s*team", r"team\s*(?:席位|母号)"],
    "Claude Pro": [r"claude\s*pro"],
    "Claude 5x": [r"claude\s*(?:max\s*)?5\s*x"],
    "Claude 20x": [r"claude\s*(?:max\s*)?20\s*x"],
    "Gemini Pro": [r"gemini\s*pro"],
    "Gemini Ultra": [r"gemini\s*ultra"],
    "Grok": [r"grok"],
    "Cursor": [r"cursor"],
    "Perplexity": [r"perplexity"],
    "Copilot": [r"copilot"],
    "Midjourney": [r"midjourney", r"\bmj\b"],
    "Gmail/Pixel": [r"gmail", r"pixel", r"谷歌邮箱"],
    "API 额度": [r"api\s*(?:额度|key)", r"\d+\s*刀\s*额度"],
    "礼品卡": [r"礼品卡", r"gift\s*card"],
}
CATEGORY_RES = {k: re.compile("|".join(v), re.I) for k, v in CATEGORIES.items()}
# 刀/u/usd 折人民币用写死的常量，不联网取汇率 —— 这里要的是可复现，不是精确
USD_CNY = 7.2
PRICE_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(r\b|rmb|元|块|¥|刀|u\b|usd|美金)", re.I)
PAIR_WINDOW = 20  # 品类词与价格相隔超过这么多字符就不算一对
# 品类和价格中间夹着时长 / 席位字眼的，是另一种货（周卡、年付、一个席位），单独归一类，别拿月价的行情去判它「低到不可能」
QUALIFIERS = [
    (re.compile(r"\d*\s*(?:天|日|周|星期|week|day)", re.I), "短期"),
    (re.compile(r"(?:1\s*)?年|12\s*个?月|year|annual", re.I), "年付"),
    (re.compile(r"季|3\s*个?月|quarter", re.I), "季付"),
    (re.compile(r"席位|成员|母号|子号|seat|slot", re.I), "席位"),
    (re.compile(r"共享|合租|拼车|车位", re.I), "合租"),
]
BOARD_NAME = "prices.json"
MIN_N = 5  # 一个品类不到这么多条报价，中位数没有意义


def to_cny(num: float, unit: str) -> float:
    return num * USD_CNY if unit.lower() in ("刀", "u", "usd", "美金") else num


def extract(text: str) -> List[Dict[str, Any]]:
    """从一段文案里抽出 (品类, 人民币价) 对。配对窗口 20 字符挡掉「价格比 GPT-5.4」这类噪音。"""
    items = []
    for pm in PRICE_UNIT_RE.finditer(text):
        num, unit = float(pm.group(1)), pm.group(2)
        before, after = (None, 10**9, ""), (None, 10**9, "")
        for cat, rx in CATEGORY_RES.items():
            for cm in rx.finditer(text):
                # 报价单一行一个品类：隔着换行的不配（上一行的品类配到下一行的价钱，整条基线就错位）
                if cm.end() <= pm.start():
                    d, mid = pm.start() - cm.end(), text[cm.end():pm.start()]
                    if d <= PAIR_WINDOW and d < before[1] and "\n" not in mid:
                        before = (cat, d, mid)
                elif cm.start() >= pm.end():
                    d, mid = cm.start() - pm.end(), text[pm.end():cm.start()]
                    if d <= PAIR_WINDOW and d < after[1] and "\n" not in mid:
                        after = (cat, d, mid)
        best, _, between = before if before[0] else after
        if best:
            for rx, tag in QUALIFIERS:
                if rx.search(between):
                    best = f"{best}·{tag}"
                    break
            items.append({"category": best, "cny": round(to_cny(num, unit), 2), "raw": pm.group(0)})
    return items


def board(quotes: List[Dict[str, Any]], min_n: int = MIN_N, today: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """按品类算：条数、中位数、MAD、红线（中位数 - 3 MAD，夹在中位数的五成到八成之间）、最低最高、几个卖家、近 7 天对比之前、最便宜的三家。"""
    today = today or f"{datetime.now(timezone.utc):%Y-%m-%d}"
    week_ago = f"{datetime.strptime(today, '%Y-%m-%d') - timedelta(days=7):%Y-%m-%d}"
    by_cat: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for q in quotes:
        by_cat[q["category"]].append(q)
    out: Dict[str, Dict[str, Any]] = {}
    for cat, qs in by_cat.items():
        vals = sorted(q["cny"] for q in qs)
        if len(vals) < min_n:
            out[cat] = {"n": len(vals), "note": "样本不够，不算行情", "values": vals[:10]}
            continue
        med = median(vals)
        mad = median(abs(v - med) for v in vals) or 1.0
        recent = [q["cny"] for q in qs if (q.get("date") or "") >= week_ago]
        older = [q["cny"] for q in qs if (q.get("date") or "") < week_ago]
        trend = round(100 * (median(recent) - median(older)) / median(older)) if len(recent) >= 3 and len(older) >= 3 and median(older) else None
        out[cat] = {
            # 红线：中位数减 3 个 MAD，但夹在中位数的五成到八成之间 —— 实测 GPT Pro 5x 几乎全报 499，MAD 接近 0，不夹的话 498 都算「低到不可能」
            "n": len(vals), "median": round(med, 2), "mad": round(mad, 2), "floor": round(max(min(med - 3 * mad, med * 0.8), med * 0.5), 2),
            "min": vals[0], "max": vals[-1], "sellers": len({q.get("seller") for q in qs}), "trend_7d_pct": trend,
            "cheapest": [{"seller": q.get("seller"), "cny": q["cny"], "raw": q["raw"]} for q in sorted(qs, key=lambda q: q["cny"])[:3]],
        }
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["n"]))


def outliers(quotes: List[Dict[str, Any]], stats: Dict[str, Dict[str, Any]], cap: int = 20) -> List[Dict[str, Any]]:
    """低于红线的报价：低到不可能，多半是假货或钓鱼。"""
    bad = [{**q, "floor": stats[q["category"]]["floor"], "median": stats[q["category"]]["median"]}
           for q in quotes if "floor" in stats.get(q["category"], {}) and q["cny"] < stats[q["category"]]["floor"]]
    return sorted(bad, key=lambda q: q["cny"] / q["median"])[:cap]


def compare(items: List[Dict[str, Any]], stats: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """一个卖家的报价逐条对行情：低于红线 / 便宜 / 差不多 / 贵 / 行情样本不够。"""
    out, seen = [], set()
    for it in items:
        key = (it["category"], it["cny"])
        if key in seen:
            continue
        seen.add(key)
        st = stats.get(it["category"]) or {}
        row = {"item": it["category"], "raw": it["raw"], "cny": it["cny"], "market_median": st.get("median"), "diff_pct": None, "below_floor": False}
        if "median" not in st:
            row["verdict"] = "行情样本不够"
        else:
            diff = round(100 * (it["cny"] - st["median"]) / st["median"])
            row["diff_pct"] = diff
            if it["cny"] < st["floor"]:
                row.update(verdict=f"低于红线 {st['floor']:g}：低到不可能", below_floor=True)
            elif diff <= -10:
                row["verdict"] = f"比行情便宜 {-diff}%"
            elif diff >= 10:
                row["verdict"] = f"比行情贵 {diff}%"
            else:
                row["verdict"] = "跟行情差不多"
        out.append(row)
    return out


def load(path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
    except Exception:
        return {}


async def _board_path(file: Optional[str], ctx: Optional[Context]):
    path, err = await writable_path(file, BOARD_NAME, ctx)
    return path, err


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Price Board", openWorldHint=True, readOnlyHint=False, destructiveHint=False, idempotentHint=True))
@validate_id("chat_id")
async def price_board(
    chat_id: Optional[Union[ChatRef, List[ChatRef]]] = None,
    query: Optional[str] = None,
    sample: int = 300,
    days: int = 30,
    save: bool = True,
    file: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> str:
    """Price board of the market: read recent posts from the groups where it trades (or a keyword search), pair every price with its product (GPT Plus, Claude 20x, ...), and give per product the median, spread, a too-cheap floor, 7-day trend and cheapest sellers, plus too-good-to-be-true offers and active sellers who never quote. Saved as prices.json so check_seller can grade a seller's quotes against it.

    Args:
        chat_id: One chat or a list where the market trades.
        query: Or search your chats for a keyword instead.
        sample: Posts to read per chat (or per search).
        days: Only posts from the last N days.
        save: Write the board into the allowed download directory for check_seller to use.
        file: Board path override (default <allowed dir>/downloads/prices.json).
    """
    from .trace import _chat_label, _post  # trace 用本模块给卡片比价，这里反过来借它读帖，只能在函数里引入

    try:
        if chat_id is None and not query:
            raise ValidationError("Give chat_id (one or a list) or query.")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        posts, notes = [], []
        if chat_id is not None:
            for cid in (chat_id if isinstance(chat_id, list) else [chat_id]):
                try:
                    ent = await resolve_entity(cid)
                except Exception as e:
                    notes.append(f"{cid} 打不开：{type(e).__name__}")
                    continue
                label = getattr(ent, "title", None) or getattr(ent, "first_name", None) or str(cid)
                posts += [_post(m, label) for m in await client.get_messages(ent, limit=sample) if (m.message or "") and m.date and m.date >= cutoff]
        else:
            async for m in client.iter_messages(None, search=query, limit=sample):
                if (m.message or "") and m.date and m.date >= cutoff:
                    posts.append(_post(m, _chat_label(m)))
        if not posts:
            return dump_json({"summary": "没读到帖子。", "posts_read": 0, "truncated": notes})
        quotes, seen, msgs, priced = [], set(), defaultdict(int), defaultdict(int)
        for p in posts:
            seller = p["sender"] if not p["sender"].startswith("id:") else p["sender_label"]
            msgs[seller] += 1
            items = extract(p["text"])
            if items:
                priced[seller] += 1
            for it in items:
                k = (seller, it["category"], it["cny"])
                if k in seen:
                    continue  # 同一个卖家刷 50 遍同一张报价单只算一条，不然中位数被刷屏的人定
                seen.add(k)
                quotes.append({**it, "seller": seller, "date": f"{p['date']:%Y-%m-%d}" if p.get("date") else None, "chat": p["chat"]})
        stats = board(quotes)
        bad = outliers(quotes, stats)
        never = sorted(
            ({"seller": s, "posts": n, "priced": priced[s]} for s, n in msgs.items() if n >= 5 and priced[s] / n < 0.2),
            key=lambda r: -r["posts"],
        )[:8]
        saved = None
        if save:
            path, err = await _board_path(file, ctx)
            if err:
                notes.append(f"行情表没存：{err}")
            else:
                path.write_text(json.dumps({"at": f"{datetime.now(timezone.utc):%Y-%m-%d}", "days": days, "posts_read": len(posts), "board": stats}, ensure_ascii=False), encoding="utf-8")
                saved = str(path)
        with_stats = [(c, s) for c, s in stats.items() if "median" in s]
        head = f"读了 {len(posts)} 条帖（{sum(priced.values())} 条带报价），{len(quotes)} 条报价归到 {len(stats)} 个品类，{len(with_stats)} 个样本够算行情。"
        if with_stats:
            head += " " + "；".join(
                f"{c} 中位 {s['median']:g} 元（{s['n']} 条，红线 {s['floor']:g}" + (f"，7 天{'涨' if s['trend_7d_pct'] > 0 else '跌'} {abs(s['trend_7d_pct'])}%" if s.get("trend_7d_pct") else "") + "）"
                for c, s in with_stats[:4]
            ) + "。"
        if bad:
            head += f" 低于红线 {len(bad)} 条，最离谱：{bad[0]['seller']} 的 {bad[0]['category']} {bad[0]['raw']}（行情 {bad[0]['median']:g}）。"
        if never:
            head += f" {len(never)} 个活跃号基本不报价（{never[0]['seller']} {never[0]['posts']} 条 0 带价）。"
        return dump_json({
            "summary": head, "since": f"{cutoff:%Y-%m-%d}", "posts_read": len(posts), "priced_posts": sum(priced.values()), "quotes": len(quotes),
            "board": stats, "outliers": bad, "never_quote": never, "saved": saved, "truncated": notes,
        })
    except Exception as e:
        return log_and_format_error("price_board", e, chat_id=chat_id, query=query)
