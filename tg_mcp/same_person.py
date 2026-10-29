"""马甲识别：一群号里哪些其实是同一个人。

骗子的核心手法就是换名字：被曝光 → 弃号 → 开新号 → 在同一个群继续卖。只按 @号 追，换个马甲负面记录就归零。
所以按「他甩不掉的东西」追：收款地址、微信 / 邮箱、整套报价单、群友明说的改名；再加几条弱信号：同一个卡网链接、
一模一样的文案、转发同一个源头、同一条曝光帖点名。聚类规则刻意笨：并查集 + 「一条强信号，或两条弱信号」，
名字同源只当助攻。输出的是证据不是判决，差一条证据的配对单独列出来让人看。规则照抄离线脚本 sockpuppet.py。
"""

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from telethon import functions

from .app import client, logger, mcp
from .common import ALIAS_PATTERNS, EMAIL_RE, WECHAT_RE, ChatRef, ValidationError, dump_json, log_and_format_error, name_kin, norm_text, resolve_entity, validate_id
from .trace import _PRICE, _find_seller, _post, handles_in, hosts_in, wallets_in

STRONG = {"crypto", "wechat", "email", "pricelist", "rename"}
LABEL = {
    "crypto": "共用收款地址", "wechat": "同一个微信", "email": "同一个邮箱", "pricelist": "一模一样的报价单", "rename": "群友说改名 / 小号",
    "url": "同一个卡网 / 链接", "textfp": "一样的文案", "fwd": "转发同一个源头", "coexposed": "同一条曝光帖点名", "namekin": "名字同源",
}
# 真正的曝光帖（而不是「碰巧含负面词的自家广告」）才有资格贡献共现信号
EXPOSURE_RE = re.compile(r"(曝光|谨防受骗|被骗提醒|踩个坑|是骗子|这是骗子|注意拉黑|大家不要上当|付款后不发货|收了U不发货|是一伙的)")
MAX_SHARED = 8  # 超过这么多人共用的值多半是公共平台（担保机器人、大卡网），不是同一个人
MIN_TEXT = 40  # 太短的文案撞车没有意义


class _UnionFind:
    """并查集：「A 和 B 是一伙、B 和 C 是一伙」要自动推出 A 和 C 同组。"""

    def __init__(self):
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def signals(posts: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Set[str]]], Dict[str, Dict[str, Any]]]:
    """每个发帖人 → 各类信号的值；顺带记下显示名 / 类型 / 条数。"""
    sig: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    who: Dict[str, Dict[str, Any]] = {}
    for p in posts:
        k, text = p["sender"], p["text"]
        w = who.setdefault(k, {"handle": k if not k.startswith("id:") else p["sender_label"], "name": p["sender_label"], "kind": p["sender_kind"], "posts": 0})
        w["posts"] += 1
        exposure = bool(EXPOSURE_RE.search(text))
        if not exposure:  # 曝光帖里的地址 / 链接是被曝光者的，不能算到发帖的受害者头上（不然受害者会被归成骗子一伙）
            sig[k]["crypto"].update(a for _, a in wallets_in(text))
            sig[k]["wechat"].update(x.lower() for x in WECHAT_RE.findall(text))
            sig[k]["email"].update(x.lower() for x in EMAIL_RE.findall(text))
            sig[k]["url"].update(hosts_in(text))
            prices = sorted({m.group(0).strip().lower() for m in _PRICE.finditer(text)})
            if len(prices) >= 3:
                sig[k]["pricelist"].add("|".join(prices[:12]))
            nt = norm_text(text)
            if len(nt) >= MIN_TEXT:
                sig[k]["textfp"].add(nt)
        if p.get("fwd"):
            sig[k]["fwd"].add(p["fwd"])
        # 群友明说「他从 @a 改成了 @b」：直接连边，不需要别的佐证
        for rx in ALIAS_PATTERNS:
            for a, b in rx.findall(text):
                tag = f"{p['chat']}#{p['id']}:{a.lower()}={b.lower()}"
                sig[f"@{a.lower()}"]["rename"].add(tag)
                sig[f"@{b.lower()}"]["rename"].add(tag)
                for h in (a, b):
                    who.setdefault(f"@{h.lower()}", {"handle": f"@{h.lower()}", "name": h, "kind": "mentioned", "posts": 0})
        # 曝光帖：「这几个 ID 是一伙的，共用这个地址」—— 整串 @ 都要，地址和链接也算到他们头上
        if exposure:
            hs = [f"@{h.lower()}" for h in handles_in(text)]
            if 2 <= len(hs) <= 10:
                addrs = {a for _, a in wallets_in(text)}
                urls = hosts_in(text)
                for h in hs:
                    sig[h]["coexposed"].add(f"{p['chat']}#{p['id']}")
                    sig[h]["crypto"].update(addrs)
                    sig[h]["url"].update(urls)
                    who.setdefault(h, {"handle": h, "name": h[1:], "kind": "mentioned", "posts": 0})
    return sig, who


def cluster(sig: Dict[str, Dict[str, Set[str]]]) -> Tuple[Dict[str, Set[str]], Dict[Tuple[str, str], Dict[str, Set[str]]], Dict[Tuple[str, str], Dict[str, Set[str]]]]:
    """共享同一个信号值的号连边：强信号一条即可，弱信号要两种。返回（组, 成案的边, 差一条证据的边）。"""
    by_value: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    for h, kinds in sig.items():
        for kind, vals in kinds.items():
            for v in vals:
                by_value[kind][v].add(h)
    edges: Dict[Tuple[str, str], Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    for kind, vmap in by_value.items():
        for v, hs in vmap.items():
            if len(hs) < 2 or len(hs) > MAX_SHARED:
                continue
            hl = sorted(hs)
            for i in range(len(hl)):
                for j in range(i + 1, len(hl)):
                    edges[(hl[i], hl[j])][kind].add(v)
    for a, b in list(edges):
        if a.startswith("@") and b.startswith("@") and name_kin(a[1:], b[1:]):
            edges[(a, b)]["namekin"].add(f"{a}~{b}")  # 撞名太常见，只能把「只有一条弱信号」的顶到门槛
    uf, kept = _UnionFind(), {}
    for pair, kinds in edges.items():
        if any(k in STRONG for k in kinds) or len(kinds) >= 2:
            uf.union(*pair)
            kept[pair] = kinds
    groups: Dict[str, Set[str]] = defaultdict(set)
    for pair in kept:
        for h in pair:
            groups[uf.find(h)].add(h)
    near = {p: k for p, k in edges.items() if p not in kept}
    return groups, kept, near


def _short(v: str) -> str:
    return v if len(v) <= 40 else v[:18] + "…" + v[-8:]


def describe(groups: Dict[str, Set[str]], kept: Dict[Tuple[str, str], Dict[str, Set[str]]], who: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for members in sorted(groups.values(), key=lambda m: -len(m)):
        ev: List[Dict[str, Any]] = []
        kinds_seen: Set[str] = set()
        for (a, b), kinds in kept.items():
            if a in members and b in members:
                for k, vs in kinds.items():
                    kinds_seen.add(k)
                    ev.append({"why": LABEL[k], "between": f"{who.get(a, {}).get('handle', a)} + {who.get(b, {}).get('handle', b)}", "values": [_short(v) for v in sorted(vs)[:3]]})
        strong = [LABEL[k] for k in kinds_seen if k in STRONG]
        out.append({
            "members": [{**who.get(m, {"handle": m, "name": m, "kind": "?", "posts": 0})} for m in sorted(members, key=lambda m: (-who.get(m, {}).get("posts", 0), m))],  # 发帖多的在前，同样多按名字
            "size": len(members), "strength": "强" if strong else "中", "signals": sorted(LABEL[k] for k in kinds_seen),
            "evidence": sorted(ev, key=lambda e: (e["why"] not in {LABEL[k] for k in STRONG}, e["why"]))[:8],
        })
    return out


def _near_rows(near: Dict[Tuple[str, str], Dict[str, Set[str]]], who: Dict[str, Dict[str, Any]], only: Optional[str] = None, cap: int = 12) -> List[Dict[str, Any]]:
    rows = []
    for (a, b), kinds in near.items():
        if only and only not in (a, b):
            continue
        rows.append({"pair": [who.get(a, {}).get("handle", a), who.get(b, {}).get("handle", b)], "signals": {LABEL[k]: [_short(v) for v in sorted(vs)[:2]] for k, vs in kinds.items()}})
    return rows[:cap]


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Same Person", openWorldHint=True, readOnlyHint=True))
@validate_id("chat_id", "handle")
async def same_person(
    chat_id: Optional[Union[ChatRef, List[ChatRef]]] = None,
    handle: Optional[ChatRef] = None,
    sample: int = 300,
    ctx: Optional[Context] = None,
) -> str:
    """Sockpuppet detection: which accounts in these groups are the same person. Links accounts that share what a scammer cannot shed — payment address, WeChat / email, an identical price list, a "he renamed to @x" post — or two weaker ties (same storefront link, identical copy, same forward source, named together in an exposure post); a shared name stem only tops up. Give handle to see that seller's own cluster.

    Args:
        chat_id: One group or a list to read (recent posts).
        handle: Focus on this seller: read their own posts too and return only their cluster and near-misses. Without chat_id, uses the groups you share with them.
        sample: Recent posts to read per group.
    """
    try:
        if chat_id is None and handle is None:
            raise ValidationError("Give chat_id (one or a list) and/or handle.")
        chats, notes, posts = [], [], []
        ent = None
        if handle is not None:
            ent = await _find_seller(handle)
            if ent is None:
                return dump_json({"summary": f"{handle} 用户名已不存在。", "handle": str(handle), "cluster": None})
        if chat_id is not None:
            for cid in (chat_id if isinstance(chat_id, list) else [chat_id]):
                try:
                    chats.append(await resolve_entity(cid))
                except Exception as e:
                    notes.append(f"{cid} 打不开：{type(e).__name__}")
        elif ent is not None:
            try:
                chats = list((await client(functions.messages.GetCommonChatsRequest(user_id=ent, max_id=0, limit=20))).chats)
            except Exception as e:
                notes.append(f"拿不到共同群：{type(e).__name__}")
        if not chats:
            raise ValidationError("No group could be opened.")
        for c in chats:
            label = getattr(c, "title", None) or str(getattr(c, "id", "?"))
            posts += [_post(m, label) for m in await client.get_messages(c, limit=sample) if (m.message or "")]
            if ent is not None:
                try:
                    posts += [_post(m, label) for m in await client.get_messages(c, limit=50, from_user=ent) if (m.message or "")]
                except Exception as e:
                    logger.warning(f"posts of {handle} in {label} failed: {e}")
        seen: Set[Tuple[str, int]] = set()
        posts = [p for p in posts if not ((p["chat"], p["id"]) in seen or seen.add((p["chat"], p["id"])))]
        if not posts:
            return dump_json({"summary": "没读到帖子。", "posts_read": 0, "truncated": notes})
        sig, who = signals(posts)
        groups, kept, near = cluster(sig)
        clusters = describe(groups, kept, who)
        base = {"posts_read": len(posts), "senders": len(who), "truncated": notes}
        if ent is not None:
            uname = getattr(ent, "username", None)
            key = f"@{uname.casefold()}" if uname else f"id:{ent.id}"
            me_handle = who.get(key, {}).get("handle", key)
            root = next((r for r, ms in groups.items() if key in ms), None)
            mine = describe({root: groups[root]}, kept, who)[0] if root else None
            others = [m for m in mine["members"] if m["handle"] != me_handle] if mine else []
            head = f"{me_handle}：读了 {len(posts)} 条帖、{len(who)} 个号。"
            if others:
                head += f" 疑似还有 {len(others)} 个号是同一个人（{mine['strength']}证据：{'、'.join(mine['signals'])}）：" + "、".join(m["handle"] for m in others[:6]) + "。"
            else:
                head += " 没找到跟他共用收款地址 / 微信 / 报价单 / 文案的号。"
            nm = _near_rows(near, who, only=key)
            if nm:
                head += f" 差一条证据的 {len(nm)} 对，要人看。"
            return dump_json({"summary": head, "handle": me_handle, "cluster": mine, "near_miss": nm, **base})
        strong = [c for c in clusters if c["strength"] == "强"]
        head = f"读了 {len(posts)} 条帖、{len(who)} 个号，聚出 {len(clusters)} 组同一人（强证据 {len(strong)} 组）。"
        if clusters:
            c = clusters[0]
            head += f" 最大一组 {c['size']} 个号：" + " ".join(m["handle"] for m in c["members"][:5]) + f"（{'、'.join(c['signals'])}）。"
        if near:
            head += f" 差一条证据的 {len(near)} 对，要人看。"
        return dump_json({"summary": head, "clusters": clusters[:20], "near_miss": _near_rows(near, who), **base})
    except Exception as e:
        return log_and_format_error("same_person", e, chat_id=chat_id, handle=handle)
