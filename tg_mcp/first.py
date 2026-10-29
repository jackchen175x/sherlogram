"""谁最早：一段话术、一个卡网、一个 @号，最早是谁、在哪个群、什么时候发的；之后扩散到了谁。

做法：按线索（网址 / @号 / 收款地址）和文案片段去搜，搜到的帖按文案指纹归成几组（字符三元组的重合度），
每组按时间排：最早那条就是时间上的源头 —— 转发帖按原帖时间和原帖作者算，所以哪怕原频道没搜到，也能指到它。
边界：全局搜索只搜得到自己加入的群和公开帖；Telegram 搜中文长句不准，所以长文案拆成短片段搜；
搜索结果从新到旧、每个词只翻 limit 条，「最早」是翻过的范围内最早，翻到底了才算真最早（结果里明说）。
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from .app import client, mcp
from .common import ChatRef, ValidationError, dump_json, log_and_format_error, norm_text, resolve_entity, validate_id
from .trace import _chat_label, _post, _public_posts, handles_in, hosts_in, wallets_in

_MAX_TERMS = 4
_SPLIT = re.compile(r"[\n\r。！？!?；;，,、：:|丨/\[\]()（）【】「」《》\"“”]+")
_MIN_FRAG, _MAX_FRAG = 6, 30
_FAR = datetime.max.replace(tzinfo=timezone.utc)


def terms_of(query: str) -> Tuple[List[str], List[str], str]:
    """（去搜的词, 其中哪些是线索, 去掉线索后剩下的文案）。线索原样搜；文案拆成短句，挑最长的两句。"""
    clues = sorted(hosts_in(query)) + [f"@{h}" for h in sorted(handles_in(query))] + sorted(a for _, a in wallets_in(query))
    rest = query
    for c in clues:
        rest = re.sub(re.escape(c.lstrip("@")), " ", rest, flags=re.I)
    rest = re.sub(r"https?://\S+|@\w+", " ", rest)
    frags = []
    for f in _SPLIT.split(rest):
        f = re.sub(r"\s+", " ", f).strip()
        if len(f) > _MAX_FRAG:
            cut = f[:_MAX_FRAG]
            f = cut.rsplit(" ", 1)[0] if " " in cut else cut  # 别把英文单词切半
        if len(f) >= _MIN_FRAG:
            frags.append(f)
    frags = sorted(set(frags), key=len, reverse=True)
    terms = (clues + frags[:2])[:_MAX_TERMS] or [query.strip()[:_MAX_FRAG]]
    return terms, clues, rest


def shingles(text: str) -> Set[str]:
    t = norm_text(text)
    return {t[i:i + 3] for i in range(len(t) - 2)} if len(t) >= 3 else ({t} if t else set())


def jaccard(a: Set[str], b: Set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def similar(a: Set[str], b: Set[str], thr: float) -> bool:
    """像不像：重合度够高；或者一个基本包在另一个里（抄了广告再加几行自己的联系方式，重合度会被拉低）。"""
    if not a or not b:
        return False
    inter = len(a & b)
    if inter / len(a | b) >= thr:
        return True
    small = min(len(a), len(b))
    return small >= 20 and inter / small >= 0.8


def _dt(d: Any) -> Optional[datetime]:
    """公开帖搜索给的是字串或时间戳，Telegram 消息给的是带时区的时间；统一成带时区的时间好排序。"""
    if isinstance(d, (int, float)):
        d = datetime.fromtimestamp(d, timezone.utc)
    elif isinstance(d, str):
        try:
            d = datetime.fromisoformat(d.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(d, datetime):
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _when(p: Dict[str, Any]) -> datetime:
    return p.get("fwd_date") or p.get("date") or _FAR


def _day(d: Optional[datetime]) -> Optional[str]:
    return f"{d:%Y-%m-%d}" if d and d is not _FAR else None


def _who(p: Dict[str, Any]) -> str:
    return p["sender_label"] if p["sender"].startswith(("id:", "name:")) else p["sender"]


def cluster(posts: List[Dict[str, Any]], similarity: float) -> List[List[Dict[str, Any]]]:
    """按文案归组：先按时间从早到晚排，每组第一条（最早的）当代表，后来的跟代表比重合度。"""
    # ponytail: 每条跟每组代表比一遍，几百条没问题；上千条再换 MinHash
    groups: List[Tuple[Set[str], List[Dict[str, Any]]]] = []
    for p in sorted(posts, key=_when):
        sh = p["_sh"]
        for rep, members in groups:
            if sh == rep or similar(sh, rep, similarity):
                members.append(p)
                break
        else:
            groups.append((sh, [p]))
    return [m for _, m in sorted(groups, key=lambda g: -len(g[1]))]


def describe(members: List[Dict[str, Any]], query_sh: Set[str], similarity: float) -> Dict[str, Any]:
    first = members[0]
    by_sender: Dict[str, Dict[str, Any]] = {}
    for p in members:
        s = by_sender.setdefault(p["sender"], {"who": _who(p), "first": _when(p), "posts": 0, "chats": set()})
        s["posts"] += 1
        s["chats"].add(p["chat"])
    spread = sorted(by_sender.values(), key=lambda s: s["first"])
    out = {
        "text": first["text"][:160], "copies": len(members), "senders": len(by_sender), "chats": len({p["chat"] for p in members}),
        "first": {
            "who": first["fwd_label"] or _who(first), "chat": first["chat"], "date": _day(_when(first)), "message_id": first["id"],
            "via_forward": f"{_who(first)} 在「{first['chat']}」转发的，原帖 {_day(first['fwd_date'])}" if first.get("fwd_date") else None,
        },
        "latest": _day(max((p["date"] for p in members if p.get("date")), default=None)),
        "spread": [{"who": s["who"], "first": _day(s["first"]), "posts": s["posts"], "chats": len(s["chats"])} for s in spread[:8]],
    }
    if query_sh:
        # 给的可能只是帖子的开头几句：查询里的片段八成都在帖子里，也算像
        out["matches_query"] = jaccard(first["_sh"], query_sh) >= similarity or len(query_sh & first["_sh"]) / len(query_sh) >= 0.6
    return out


async def _gather(terms: List[str], entities: List[Any], limit: int) -> Tuple[List[Dict[str, Any]], List[str]]:
    posts, seen, notes = [], set(), []
    for term in terms:
        for ent in entities:
            label = getattr(ent, "title", None) or getattr(ent, "first_name", None)
            n = 0
            async for m in client.iter_messages(ent, search=term, limit=limit):
                n += 1
                p = _post(m, label or _chat_label(m))
                key = (p["chat"], p["id"])
                if key in seen:
                    continue
                seen.add(key)
                fwd = getattr(m, "fwd_from", None)
                p["date"], p["fwd_date"] = _dt(p["date"]), _dt(getattr(fwd, "date", None)) if fwd else None
                posts.append(p)
            if n >= limit:
                notes.append(f"「{term}」" + (f"在「{label}」" if label else "") + f"只翻了最近 {limit} 条，更早的可能没翻到")
        if entities == [None]:
            try:
                pub, status = await _public_posts(term, 50)
            except Exception as e:
                pub, status = [], type(e).__name__
            if status:
                notes.append(f"「{term}」公开频道搜索没用上：{status}")
            for p in pub:
                key = (p["chat"], p["id"])
                if key not in seen:
                    seen.add(key)
                    p["date"], p["fwd_date"] = _dt(p["date"]), None
                    posts.append(p)
    for p in posts:
        p["_sh"] = shingles(p["text"])
    return posts, notes


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Who First", openWorldHint=True, readOnlyHint=True))
@validate_id("chat_id")
async def who_first(
    query: str,
    chat_id: Optional[Union[ChatRef, List[ChatRef]]] = None,
    limit: int = 200,
    similarity: float = 0.5,
    ctx: Optional[Context] = None,
) -> str:
    """Who said it first: give an ad text, a storefront domain, an @handle or a wallet; searches your chats and public channels, groups the hits by text fingerprint, and for each group tells who posted it first, where and when (forwards count by their original date and author), then who copied it and from when. Also the first mention of each clue.

    Args:
        query: Ad copy, a domain, @handle or wallet; long text is split into short fragments (Telegram search is word-based).
        chat_id: One chat or a list to search inside instead of all your chats.
        limit: Messages scanned per search term (newest first); "first" means within this window unless the search was exhausted.
        similarity: 0-1, how alike two texts must be to count as copies of each other.
    """
    try:
        if not query or not query.strip():
            raise ValidationError("query is required.")
        terms, clues, rest = terms_of(query)
        entities: List[Any] = [None]
        notes: List[str] = []
        if chat_id is not None:
            entities = []
            for cid in (chat_id if isinstance(chat_id, list) else [chat_id]):
                try:
                    entities.append(await resolve_entity(cid))
                except Exception as e:
                    notes.append(f"{cid} 打不开：{type(e).__name__}")
            if not entities:
                raise ValidationError("None of the chats could be opened.")
        posts, more = await _gather(terms, entities, limit)
        notes += more
        if clues and len(terms) == len(clues):
            # 只给了线索（域名 / @号 / 地址）：搜索会把「shopa.pro」拆成 shopa 和 pro 去匹配，混进一堆无关帖，正文里没这个线索的不要
            posts = [p for p in posts if any(c.lstrip("@").casefold() in p["text"].casefold() for c in clues)]
        if not posts:
            return dump_json({"summary": "没搜到。换个更短、更有辨识度的片段，或者给 chat_id。", "terms": terms, "scanned": 0, "truncated": notes})
        query_sh = shingles(query) if len(norm_text(rest)) >= 8 else set()  # 给的是文案才有「像不像」可比；只给个域名就不比
        groups = [describe(g, query_sh, similarity) for g in cluster(posts, similarity)]
        if query_sh:
            groups.sort(key=lambda g: (not g["matches_query"], -g["copies"]))
        first_mention: Dict[str, Any] = {}
        for c in clues:
            low = c.lstrip("@").casefold()
            hits = [p for p in posts if low in p["text"].casefold()]
            if hits:
                h = min(hits, key=_when)
                first_mention[c] = {
                    "date": _day(_when(h)), "who": h["fwd_label"] or _who(h), "chat": h["chat"], "message_id": h["id"],
                    "by_self": c.startswith("@") and h["sender"] == c.casefold(),
                }
        top = groups[0]
        head = f"搜了 {len(terms)} 个词、翻到 {len(posts)} 条帖，归成 {len(groups)} 组文案。"
        if query_sh and not top["matches_query"]:
            head += " 没有一组跟你给的文案像，下面按条数排。"
        head += f" 最大一组 {top['copies']} 条、{top['senders']} 个号在发，最早是 {top['first']['who']} {top['first']['date']} 在「{top['first']['chat']}」发的"
        head += f"（{top['first']['via_forward']}）。" if top["first"]["via_forward"] else "。"
        for c, fm in first_mention.items():
            head += f" {c} 最早 {fm['date']} 出现在「{fm['chat']}」，" + ("是他自己发的。" if fm["by_self"] else f"{fm['who']} 发的。")
        if notes:
            head += " 注意：" + "；".join(notes[:2]) + "。"
        return dump_json({"summary": head, "terms": terms, "scanned": len(posts), "groups": groups[:10], "first_mention": first_mention, "truncated": notes})
    except Exception as e:
        return log_and_format_error("who_first", e, query=query[:60], chat_id=chat_id)
