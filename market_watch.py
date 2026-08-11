#!/usr/bin/env python3
"""定点复查一批 Telegram 卖家，产出可跨时间比对的窄快照。

为什么是脚本而不是第 99 个 MCP 工具：它是批处理，跑几分钟，产出是文件。
作为 MCP 工具会阻塞 stdio 直到客户端超时；作为脚本，agent 用 Bash 调、读摘要，
上下文成本低一个量级。

为什么是"定点复查"而不是全网递归重爬：4-29 那次 depth=5 / 50k 消息的全遍历
触发过 18 小时的 ResolveUsername FloodWait。真正需要回答的问题只有
"上次点过名的这十几个对象，现在怎么样了"，几十秒就够。

    uv run python3 market_watch.py --targets <targets.json> --out <snapshot.jsonl>
    uv run python3 market_watch.py --diff <旧.jsonl> <新.jsonl>

窄 schema 是自己定义的、只此一份，所以天然可比 —— 4-29 和 4-30 两份快照对不上，
正是因为各写各的 schema。
"""
import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import main as m  # noqa: E402  复用已有的 client / resolve_entity / 线索抽取

from telethon import errors  # noqa: E402

PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(r|rmb|元|块|¥|刀|u|usd|美金)", re.I)


def _prices(text: str) -> list:
    """抓价格片段。这里只做粗提取，不做品类绑定和币种归一（那是后续的事）——
    目的仅仅是"和上次比，报价变了没有"，锚点粗一点不影响 diff。"""
    return [f"{n}{unit}" for n, unit in PRICE_RE.findall(text or "")]


async def collect_user_posts(entity, source_chats: list, msg_limit: int) -> list:
    """个人卖家的广告发在群里，不在"和他的对话"里。

    对 User 直接 get_messages 取到的是自己与他的私聊记录 —— 通常是空的，
    于是工具会报"活着但没有任何数据"，正是最容易被误读成"这人不活跃了"的结果。
    真正要做的是在语料群里按发送者过滤（main.list_messages 的 from_user 同源）。
    注意不能反过来搜他的 @handle：刷屏卖家自己的广告里根本不含自己的 handle。
    """
    posts = []
    for chat in source_chats:
        try:
            got = await m.client.get_messages(chat, limit=msg_limit, from_user=entity)
        except Exception:
            continue  # 某个群读不了不该拖垮整个对象
        posts.extend(got)
        if len(posts) >= msg_limit:
            break
    return posts


async def check_one(t: dict, msg_limit: int, source_chats: list = ()) -> dict:
    """检查单个对象。任何失败都记录成状态而不是抛出 —— 一个死掉的卖家不该中断整轮。"""
    username = t["username"]
    row = {
        "username": username,
        "tier_20260429": t.get("tier_20260429"),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "alive": None,
        "status": None,
    }
    # 直接用 ResolveUsernameRequest 而不是 main.resolve_entity：
    #  1) 后者会把 UsernameNotOccupiedError 吞成 ValueError（telethon get_entity 的行为），
    #     "卖家消失了"这条最有价值的信号会被误判成"解析失败"——最坏的失败模式；
    #  2) 后者解析失败时会退回拉全量 dialogs，对十几个目标就是十几次全量拉取；
    #  3) 它的响应自带新鲜的 User/Channel 对象，信任标记不经过任何缓存。
    try:
        resolved = await m.client(
            m.functions.contacts.ResolveUsernameRequest(username=username)
        )
        found = list(resolved.users) + list(resolved.chats)
        if not found:
            row.update(alive=False, status="username_gone")
            return row
        entity = found[0]
    except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
        row.update(alive=False, status="username_gone")
        return row
    except errors.FloodWaitError as e:
        row.update(alive=None, status=f"flood_wait_{e.seconds}s")
        raise FloodStop(e.seconds) from e
    except ValueError as e:
        # 兜底：telethon 某些路径仍会把"查无此人"包成 ValueError
        if "No user has" in str(e) or "Cannot find any entity" in str(e):
            row.update(alive=False, status="username_gone")
        else:
            row.update(alive=None, status=f"resolve_failed: ValueError: {e}"[:200])
        return row
    except Exception as e:
        row.update(alive=None, status=f"resolve_failed: {type(e).__name__}: {e}"[:200])
        return row

    row["alive"] = True
    row["status"] = "ok"
    row["peer_id"] = getattr(entity, "id", None)
    row["name"] = getattr(entity, "title", None) or " ".join(
        x for x in (getattr(entity, "first_name", ""), getattr(entity, "last_name", "")) if x
    ).strip()
    # 官方标记：高精度低召回，别当护栏用，但零成本
    for flag in ("scam", "fake", "restricted", "verified"):
        if getattr(entity, flag, False):
            row[flag] = True

    # 换了 id 但占着同一个 username = 壳被转手/翻新，比任何截面特征都硬
    old_id = (t.get("baseline") or {}).get("peer_id")
    if old_id and row["peer_id"] and abs(old_id) != abs(row["peer_id"]):
        row["id_rebound"] = {"was": old_id, "now": row["peer_id"]}

    is_user = entity.__class__.__name__ == "User"
    try:
        if is_user:
            msgs = await collect_user_posts(entity, source_chats, msg_limit)
            row["sampled_from"] = "source_chats" if msgs else "source_chats(empty)"
        else:
            msgs = await m.client.get_messages(entity, limit=msg_limit)
            row["sampled_from"] = "own_channel"
    except errors.FloodWaitError as e:
        raise FloodStop(e.seconds) from e
    except Exception as e:
        row["status"] = f"alive_but_unreadable: {type(e).__name__}"[:120]
        return row

    texts = [(mm.message or "") for mm in msgs]
    dates = [mm.date for mm in msgs if mm.date]
    row["last_msg_date"] = max(dates).strftime("%Y-%m-%d") if dates else None
    row["msgs_sampled"] = len(msgs)
    if dates:
        row["days_since_last_msg"] = (datetime.now(timezone.utc) - max(dates)).days

    clues = m._extract_public_post_clues([{"text": x} for x in texts])
    row["domains"] = [d["value"] for d in clues["linked_domains"][:10]]
    row["mentions"] = [x["value"] for x in clues["mentions"][:10]]
    seen, prices = set(), []
    for x in texts:
        for p in _prices(x):
            if p not in seen:
                seen.add(p)
                prices.append(p)
    row["prices"] = prices[:20]

    await check_history(row, t)
    return row


async def check_history(row: dict, t: dict) -> None:
    """抽查上次记录过的帖子是否还在。

    "近期没发言" 和 "过往帖子被整片删掉" 在活跃度上看起来一样，含义却完全相反：
    前者是沉寂，后者是痕迹被抹除（退场前清场、或被群管清理）。实测 @demo_seller1 在
    4-29 的 332 条记录今天一条不剩，而那个群本身仍在正常发消息 —— 只看
    last_msg_date 会把它读成"最近不活跃"，那是错的结论。
    """
    anchors = (t.get("baseline") or {}).get("post_anchors") or []
    if not anchors:
        return
    by_chat = {}
    for a in anchors:
        by_chat.setdefault(a["chat"], []).append(a["message_id"])

    alive_n = total = 0
    for chat_name, ids in by_chat.items():
        if not chat_name:
            continue
        try:
            chat = await m.resolve_entity(chat_name)
            got = await m.client.get_messages(chat, ids=ids)
        except Exception:
            continue
        total += len(ids)
        alive_n += sum(1 for g in got if g is not None)

    if total:
        row["past_posts_checked"] = total
        row["past_posts_alive"] = alive_n
        if alive_n == 0:
            row["history_wiped"] = True


class FloodStop(Exception):
    """撞到 FloodWait 就整轮停下 —— 继续打只会把等待时间叠成几十小时。"""

    def __init__(self, seconds: int):
        self.seconds = seconds


async def scan(
    targets_path: Path, out_path: Path, msg_limit: int, delay: float, source_chats: list
) -> None:
    doc = json.loads(targets_path.read_text())
    targets = doc["targets"]
    await m.client.connect()
    if not await m.client.is_user_authorized():
        sys.exit("session 未授权，先跑 session_string_generator.py")

    # 语料群解析一次，供所有 User 型目标复用（每个目标各解析一次是十几倍的浪费）
    resolved_sources = []
    for c in source_chats:
        try:
            resolved_sources.append(await m.resolve_entity(c))
        except Exception as e:
            print(f"  ! 语料群 {c} 解析失败，跳过: {type(e).__name__}")
    if source_chats:
        print(f"语料群 {len(resolved_sources)}/{len(source_chats)} 可用\n")

    rows, stopped = [], None
    for i, t in enumerate(targets, 1):
        try:
            row = await check_one(t, msg_limit, resolved_sources)
        except FloodStop as fs:
            stopped = fs.seconds
            print(f"  !! FloodWait {fs.seconds}s，停止本轮（已完成 {len(rows)}/{len(targets)}）")
            break
        rows.append(row)
        mark = {"ok": "·", "username_gone": "✗"}.get(row["status"], "?")
        print(f"  [{i:>2}/{len(targets)}] {mark} @{row['username']:<22} {row['status']}")
        if i < len(targets):
            await asyncio.sleep(delay)  # ResolveUsername 是已知封号源，别连打

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    await m.client.disconnect()

    gone = sum(1 for r in rows if r["status"] == "username_gone")
    print(f"\n写入 {out_path}")
    print(f"共 {len(rows)} 条：存活 {sum(1 for r in rows if r['alive'])}，消失 {gone}")
    if stopped:
        print(f"⚠️ 本轮因 FloodWait({stopped}s) 提前结束，剩余目标未检查")


def load_jsonl(p: Path) -> dict:
    return {json.loads(l)["username"]: json.loads(l) for l in p.read_text().splitlines() if l.strip()}


def diff(old_path: Path, new_path: Path) -> None:
    old, new = load_jsonl(old_path), load_jsonl(new_path)
    print(f"=== {old_path.name} → {new_path.name} ===\n")

    for u in sorted(set(new) - set(old)):
        print(f"[新增] @{u}")
    for u in sorted(set(old) - set(new)):
        print(f"[移除] @{u}")

    for u in sorted(set(old) & set(new)):
        o, n = old[u], new[u]
        changes = []
        if o.get("alive") and not n.get("alive"):
            changes.append(f"消失了（{n.get('status')}）")
        if not o.get("alive") and n.get("alive"):
            changes.append("又活了")
        if n.get("id_rebound"):
            changes.append(f"username 换了主人 {n['id_rebound']['was']} → {n['id_rebound']['now']}")
        for field in ("domains", "prices"):
            a, b = set(o.get(field) or []), set(n.get(field) or [])
            if a - b:
                changes.append(f"{field} 少了: {sorted(a - b)[:6]}")
            if b - a:
                changes.append(f"{field} 多了: {sorted(b - a)[:6]}")
        for flag in ("scam", "fake", "restricted"):
            if n.get(flag) and not o.get(flag):
                changes.append(f"被官方标记 {flag}")
        if o.get("name") != n.get("name") and n.get("name"):
            changes.append(f"改名: {o.get('name')!r} → {n.get('name')!r}")
        if changes:
            print(f"[变化] @{u}  (4-29 分档 {n.get('tier_20260429')})")
            for c in changes:
                print(f"        - {c}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--targets", type=Path, help="目标基线 JSON")
    ap.add_argument("--out", type=Path, help="快照输出 JSONL")
    ap.add_argument("--msg-limit", type=int, default=30, help="每个对象取多少条最近消息（默认 30）")
    ap.add_argument("--delay", type=float, default=2.0, help="每个对象之间的间隔秒数（默认 2）")
    ap.add_argument("--diff", nargs=2, type=Path, metavar=("旧", "新"), help="比对两份快照")
    ap.add_argument(
        "--source-chats",
        default="demo_group,demo_forum_channel,demo_ai_channel,demo_channel",
        help="逗号分隔的语料群：个人卖家的广告发在这里，不在与他的私聊里。"
        "默认取 4-29 扫描中线索量最大的几个，保证与那份基线可比。",
    )
    args = ap.parse_args()

    if args.diff:
        diff(*args.diff)
    elif args.targets and args.out:
        sources = [c.strip() for c in args.source_chats.split(",") if c.strip()]
        asyncio.run(scan(args.targets, args.out, args.msg_limit, args.delay, sources))
    else:
        ap.error("要么给 --targets 和 --out，要么给 --diff")


if __name__ == "__main__":
    main()
