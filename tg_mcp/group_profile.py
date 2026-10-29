"""群画像：给一个群（或频道），读最近几百条帖，一眼看出它是干什么的、谁最活跃、广告占多少、几个机器人、哪些号疑似同一个人。

全部从一页页消息里算出来，不逐个解析用户名（那是限速源）；机器人只认官方的 is_bot 标记和「经由机器人发的帖」。
"""

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from telethon import functions, types

from .app import client, logger, mcp
from .common import NEGATIVE_WORDS, ChatRef, dump_json, log_and_format_error, name_kin, norm_text, resolve_entity, validate_id
from .report import render_html
from .trace import _EPOCH, _PRICE, _entity_key, _first_msg, _post, _slug, _write_report, handles_in, hosts_in, wallets_in

_SELL = re.compile(r"代充|出售|批发|货源|卡网|秒发|有货|上新|接单|直充|代购|找我|私聊|下单", re.I)
_TOP = 10


def is_ad(text: str) -> bool:
    """广告 = 带报价，或带卡网链接，或叫卖词 + 联系方式（@号）。"""
    return bool(_PRICE.search(text) or hosts_in(text) or (_SELL.search(text) and handles_in(text)))


def _pct(a: int, b: int) -> Optional[int]:
    return round(100 * a / b) if b else None


def _day(d: Any) -> Optional[str]:
    return f"{d:%Y-%m-%d}" if isinstance(d, datetime) else None


def sender_rows(posts: List[Dict[str, Any]], top: int = _TOP) -> List[Dict[str, Any]]:
    by: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in posts:
        by[p["sender"]].append(p)
    rows = []
    for key, ps in by.items():
        texts = [p["text"] for p in ps]
        blob = "\n".join(texts)
        rows.append({
            "key": key, "handle": key if not key.startswith("id:") else ps[0]["sender_label"], "name": ps[0]["sender_label"], "kind": ps[0]["sender_kind"],
            "posts": len(ps), "share": _pct(len(ps), len(posts)), "distinct_texts": len({norm_text(t) for t in texts if norm_text(t)}),
            "ads": sum(1 for t in texts if is_ad(t)), "last_post": _day(max((p["date"] for p in ps if p.get("date")), default=None)),
            "sites": [h for h, _ in Counter(h for t in texts for h in hosts_in(t)).most_common(3)],
            "mentions": [h for h, _ in Counter(h for t in texts for h in handles_in(t)).most_common(3)],
            "wallets": sorted({a for _, a in wallets_in(blob)}),
        })
    return sorted(rows, key=lambda r: (-r["posts"], -r["ads"]))[:top]  # 同样条数的，广告多的排前面


def related_pairs(rows: List[Dict[str, Any]], posts: List[Dict[str, Any]], cap: int = 10) -> List[Dict[str, Any]]:
    """活跃号两两比：共用收款地址 > 同一个卡网 > 名字同源 > 发过一样的文案。只比发过 2 条以上的号。"""
    rows = [r for r in rows if r["posts"] >= 2]
    fps: Dict[str, Set[str]] = defaultdict(set)
    for p in posts:
        t = norm_text(p["text"])
        if len(t) >= 20:
            fps[p["sender"]].add(t)
    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            w, st = set(a["wallets"]) & set(b["wallets"]), set(a["sites"]) & set(b["sites"])
            if w:
                why = "共用收款地址 " + sorted(w)[0][:6] + "…"
            elif st:
                why = "同一个卡网 " + sorted(st)[0]
            elif a["key"].startswith("@") and b["key"].startswith("@") and name_kin(a["key"][1:], b["key"][1:]):
                why = "名字同源"
            elif fps[a["key"]] & fps[b["key"]]:
                why = "发过一样的文案"
            else:
                continue
            out.append({"a": a["handle"], "b": b["handle"], "why": why})
    return out[:cap]


def negative(posts: List[Dict[str, Any]]) -> Dict[str, Any]:
    bad = [p for p in posts if any(w in p["text"] for w in NEGATIVE_WORDS)]
    accused: Counter = Counter(f"@{h}" for p in bad for h in handles_in(p["text"]))
    return {"posts": len(bad), "ratio": _pct(len(bad), len(posts)), "most_accused": [{"handle": h, "times": n} for h, n in accused.most_common(5)]}


def verdict(kind: str, ad_ratio: Optional[int], top3: Optional[int], bots_share: Optional[int]) -> str:
    if kind == "channel":
        return "单向发布的频道" + ("，内容基本是广告" if (ad_ratio or 0) >= 60 else "")
    if (ad_ratio or 0) >= 60:
        return "卖场：广告为主" + ("，且被少数几个号刷屏" if (top3 or 0) >= 60 else "")
    if (bots_share or 0) >= 40:
        return "机器人刷屏的群"
    if (ad_ratio or 0) < 20:
        return "聊天讨论为主"
    return "聊天和广告混着"


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Group Profile", openWorldHint=True, readOnlyHint=True))
@validate_id("chat_id")
async def group_profile(chat_id: ChatRef, sample: int = 500, report: bool = False, ctx: Optional[Context] = None) -> str:
    """Profile a group or channel from its recent posts: what it is for, activity, ad share, top posters, bot share, suspected same-person pairs (shared wallet / storefront / name stem / identical copy), who gets accused, main storefronts and prices.

    Args:
        chat_id: @username or ID of the group / channel.
        sample: How many recent messages to read (pages of 100); only those with text count as posts.
        report: Also write a shareable HTML page into the allowed download directory.
    """
    try:
        ent = await resolve_entity(chat_id)
        key, label, kind = _entity_key(ent)
        info: Dict[str, Any] = {"id": ent.id, "title": label, "username": getattr(ent, "username", None), "kind": kind}
        opacity: List[str] = []
        try:
            fc = (await client(functions.channels.GetFullChannelRequest(channel=ent))).full_chat
            if fc.about:
                info["about"] = fc.about[:300]
            info["members"] = fc.participants_count
            if getattr(ent, "noforwards", False):
                opacity.append("禁止转发/截图外传")
            if getattr(fc, "hidden_prehistory", False):
                opacity.append("新成员看不到入群前历史")
            if kind == "channel" and not getattr(fc, "linked_chat_id", None):
                opacity.append("没有评论区")
        except Exception as e:
            logger.warning(f"full chat failed for {chat_id}: {e}")
        if opacity:
            info["opacity"] = opacity
        try:
            first = await _first_msg(ent, offset_date=_EPOCH)
            info["first_post"] = _day(getattr(first, "date", None))
        except Exception as e:
            logger.warning(f"first post failed for {chat_id}: {e}")
        bot_members = None
        if kind != "channel":
            try:
                bot_members = len(await client.get_participants(ent, filter=types.ChannelParticipantsBots()))
            except Exception as e:
                logger.warning(f"bot members failed for {chat_id}: {e}")

        raw = await client.get_messages(ent, limit=sample)
        posts = [_post(m, label) for m in raw if (m.message or "")]  # 进群退群提示、纯图纯贴纸没有文字，不算帖
        if not posts:
            return dump_json({"summary": f"「{label}」读不到带文字的帖子。", "chat": info, "sample": {"messages_read": len(raw), "posts": 0}})
        dates = [p["date"] for p in posts if p.get("date")]
        span = max(1, (max(dates) - min(dates)).days) if dates else None
        texts = [p["text"] for p in posts]
        ads = sum(1 for t in texts if is_ad(t))
        rows = sender_rows(posts)
        bots = [r for r in rows if r["kind"] == "bot"]
        bot_posts = sum(1 for p in posts if p["sender_kind"] == "bot")
        top3 = _pct(sum(r["posts"] for r in rows[:3]), len(posts))
        pairs = related_pairs(rows, posts)
        neg = negative(posts)
        blob = "\n".join(texts)
        fps = {norm_text(t) for t in texts if len(norm_text(t)) >= 20}
        sample_info = {
            "messages_read": len(raw), "posts": len(posts), "from": _day(min(dates)) if dates else None, "to": _day(max(dates)) if dates else None, "days": span,
            "per_day": round(len(posts) / span, 1) if span else None, "senders": len({p["sender"] for p in posts}),
            "bots_posting": len({p["sender"] for p in posts if p["sender_kind"] == "bot"}), "bot_posts_share": _pct(bot_posts, len(posts)),
            "via_bot_posts": sum(1 for p in posts if p["via_bot"]), "bot_members": bot_members,
            "forwarded": sum(1 for p in posts if p["fwd"]),
        }
        ad_info = {
            "posts": ads, "ratio": _pct(ads, len(posts)), "priced": sum(1 for t in texts if _PRICE.search(t)),
            "with_links": sum(1 for t in texts if hosts_in(t)), "distinct_texts": len(fps), "copies_per_text": round(len(posts) / len(fps), 1) if fps else None,
            "top_domains": [h for h, _ in Counter(h for t in texts for h in hosts_in(t)).most_common(8)],
            "top_mentions": [f"@{h}" for h, _ in Counter(h for t in texts for h in handles_in(t)).most_common(8)],
            "prices": list(dict.fromkeys(m.group(0).strip() for m in _PRICE.finditer(blob)))[:10],
            "wallets": sorted({a for _, a in wallets_in(blob)})[:5],
        }
        what = verdict(kind, ad_info["ratio"], top3, sample_info["bot_posts_share"])
        head = f"「{label}」{what}。" + (f"{info['members']} 人，" if info.get("members") else "") + (f"{info['first_post']} 建，" if info.get("first_post") else "")
        head += f"近 {len(posts)} 条帖跨 {span} 天（每天约 {sample_info['per_day']} 条），{sample_info['senders']} 个号在发，前 3 个号占 {top3}%；"
        head += f"广告占 {ad_info['ratio']}%（{ad_info['priced']} 条带报价、{ad_info['with_links']} 条带链接）"
        if bots or bot_members:
            head += f"；机器人 {bot_members if bot_members is not None else len(bots)} 个，发了 {sample_info['bot_posts_share']}% 的帖"
        if pairs:
            head += f"；疑似同一人 {len(pairs)} 对"
        if neg["posts"]:
            head += f"；骂人的帖 {neg['posts']} 条" + (f"，被点名最多 {neg['most_accused'][0]['handle']}（{neg['most_accused'][0]['times']} 次）" if neg["most_accused"] else "")
        if ad_info["top_domains"]:
            head += "。主要卡网：" + "、".join(ad_info["top_domains"][:3])
        head += "。"
        out = {
            "summary": head, "verdict": what, "chat": info, "sample": sample_info, "ads": ad_info, "top_senders": rows, "concentration_top3": top3,
            "related_pairs": pairs, "negative": neg,
        }
        if report:
            name = f"group-{_slug(info.get('username') or label)}-{datetime.now(timezone.utc).astimezone():%Y%m%d-%H%M}.html"
            page = {"summary": head, "sellers": rows}
            out["report"] = await _write_report(name, render_html(page, None, None, title=f"群画像：{label}"), ctx)
        return dump_json(out)
    except Exception as e:
        return log_and_format_error("group_profile", e, chat_id=chat_id)
