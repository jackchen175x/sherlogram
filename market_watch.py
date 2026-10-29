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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from telethon import errors, functions  # noqa: E402

from tg_mcp.app import client  # noqa: E402  复用常驻那套配置 / 登录凭证
from tg_mcp.common import NEGATIVE_WORDS, extract_public_post_clues, resolve_entity  # noqa: E402

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
            got = await client.get_messages(chat, limit=msg_limit, from_user=entity)
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
        resolved = await client(
            functions.contacts.ResolveUsernameRequest(username=username)
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
            msgs = await client.get_messages(entity, limit=msg_limit)
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

    clues = extract_public_post_clues([{"text": x} for x in texts])
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
            chat = await resolve_entity(chat_name)
            got = await client.get_messages(chat, ids=ids)
        except Exception:
            continue
        total += len(ids)
        alive_n += sum(1 for g in got if g is not None)

    if total:
        row["past_posts_checked"] = total
        row["past_posts_alive"] = alive_n
        if alive_n == 0:
            row["history_wiped"] = True


# 品类词典：直接抄三份报告里已经出现过的枚举，不自己发明分类
# 品类表、报价抽取、汇率常量都搬到了 tg_mcp/prices.py（price_board 工具和卖家卡片共用），这里只引入
from tg_mcp.prices import CATEGORIES, CATEGORY_RES, PAIR_WINDOW, PRICE_UNIT_RE, USD_CNY  # noqa: E402,F401
from tg_mcp.prices import extract as extract_priced_items  # noqa: E402
from tg_mcp.prices import to_cny as _to_cny  # noqa: E402,F401


def price_report(corpus_paths) -> dict:
    """按品类算中位数和 MAD，标出"低到不可能"的报价。

    用中位数 + MAD 而不是均值/标准差：这个市场是长尾，几条离谱高价就能把均值拉飞。
    「报价突然低于群内常见价太多」是识别假货最有效的单一规则，但它需要同品类基线。

    同时算每个卖家的 price_transparency —— 很多卖家**故意不在群里报价**
    （「详情卡网」「私聊我」），那不是数据缺失，那正是六维里「是否有明确价格」
    这一维该扣分的地方。不要去替他把价格挖出来。
    """
    from statistics import median

    by_cat = defaultdict(list)
    seller_msgs, seller_priced = Counter(), Counter()
    samples = []
    for p in corpus_paths:
        d = json.loads(Path(p).read_text())
        for r in d.get("sale_records", []):
            text = r.get("text_snippet") or ""
            u = ((r.get("sender") or {}).get("username") or "").lower()
            if u:
                seller_msgs[u] += 1
            items = extract_priced_items(text)
            if items and u:
                seller_priced[u] += 1
            for it in items:
                by_cat[it["category"]].append(it["cny"])
                samples.append({**it, "seller": u, "date": (r.get("date") or "")[:10]})

    stats = {}
    for cat, vals in by_cat.items():
        if len(vals) < 5:
            continue  # 样本太少，中位数没有意义
        med = median(vals)
        mad = median([abs(v - med) for v in vals]) or 1.0
        stats[cat] = {"n": len(vals), "median": round(med, 2), "mad": round(mad, 2),
                      "floor": round(max(med - 3 * mad, med * 0.5), 2)}

    outliers = [s for s in samples
                if s["category"] in stats and s["cny"] < stats[s["category"]]["floor"]]

    transparency = {
        u: {"msgs": n, "with_price": seller_priced[u],
            "price_transparency": round(seller_priced[u] / n, 2)}
        for u, n in seller_msgs.most_common(40) if n >= 5
    }
    return {"category_stats": stats, "outliers": outliers[:40],
            "price_transparency": transparency}


# 全市场同时出事的信号词。这些跟单个卖家的信誉无关，但会让所有 A 档在 24 小时内失效。
REGIME_WORDS = [
    "重置额度", "官方下场", "大面积掉", "全网都掉", "批量退款",
    "暂停接单", "不再接单", "全线掉", "都掉了",
]
# 光有信号词会误报：实测「重置额度」命中的是「谁能弄 gpt 额度重置」「每个付款周期
# 能用几次重置额度」这类正常聊天。原始证据里真正有判别力的是**规模**——
# 「凌晨 重置额度 官方下场 全网所有渠道充值 Pro 基本掉了 80%」。
# 所以命中之后还要求文本里出现规模词，否则不算市场级事件。
# 注意不要放「批量」这类词：卖家广告里「批量出 claude max」满天飞，会把刷屏广告
# 全部误判成市场事件（实测就是如此，11 条命中全是同一条广告）。
REGIME_SCALE_RE = re.compile(
    r"全网|全线|所有渠道|各家都|大面积|集体|都掉了|一起掉|掉了\s*\d+\s*%?|官方下场|封了一批"
)


async def check_regime(source_chats: list, hours: int = 72) -> dict:
    """市场级降级因子：最近 72 小时有没有"全市场一起掉链子"。

    真实教训：2026-04-28 群里有人说「凌晨 重置额度 官方下场 全网所有渠道充值 Pro
    基本掉了 80%」，**次日**的性价比报告仍把某家列为 A 档首选；而同期另一家已经在
    发退款通知了。这类信号不属于任何一个卖家，却会让所有 A 档在 24 小时内失效。

    要求跨 ≥2 个独立群命中，避免一个群里几个人聊同一件事就触发。
    """
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    hits, chats_hit = [], set()
    for name in source_chats:
        try:
            ent = await resolve_entity(name)
        except Exception:
            continue
        for word in REGIME_WORDS:
            try:
                async for msg in client.iter_messages(ent, search=word, limit=10):
                    if not msg.date or msg.date.timestamp() < cutoff:
                        break
                    text = msg.message or ""
                    # Telegram 全文搜索会分词：搜「批量退款」会命中同时含「批量」和
                    # 「退款」的普通广告。必须回头确认原词逐字出现。
                    if word not in text:
                        continue
                    if not REGIME_SCALE_RE.search(text):
                        continue  # 有信号词但没有规模词 —— 是日常聊天，不是市场事件
                    hits.append({
                        "chat": name, "word": word, "message_id": msg.id,
                        "date": msg.date.strftime("%Y-%m-%d %H:%M"),
                        "text": (msg.message or "")[:200],
                    })
                    chats_hit.add(name)
            except Exception:
                continue

    # 同一条刷屏广告发几十遍不该算几十次事件：按归一化文案去重
    uniq, seen_text = {}, set()
    for h in hits:
        fp = re.sub(r"\s+", "", h["text"])[:120]
        if fp in seen_text:
            continue
        seen_text.add(fp)
        uniq[(h["chat"], h["message_id"])] = h
    n = len(uniq) if len(chats_hit) >= 2 else 0
    level = 2 if n >= 5 else (1 if n >= 2 else 0)
    return {
        "level": level,
        "window_hours": hours,
        "distinct_chats": len(chats_hit),
        "hits": list(uniq.values())[:12],
        "meaning": {
            0: "正常",
            1: "观察：出现市场级异动，试单前再看一眼",
            2: "冻结：市场级掉订阅事件进行中，本轮所有 A 档强制降 B，不建议试单",
        }[level],
    }


def classify_domains(hosts: set) -> dict:
    """卡网域名分类。「打不开」必须区分成两件事：

      · 服务器挂了 / 域名过期  →  这家可能只是不做了
      · 被 Cloudflare 判定为钓鱼  →  这是第三方权威给出的诈骗认定

    实测教训：pay.ldxp.cn 正常返回「链动小铺」，而 play.ldxp.demo-fake.cn、pay.ldxp-demo.me
    等 5 个仿冒域名全部返回 Cloudflare 的 Suspected Phishing 拦截页 —— 一字之差。
    只按"能不能打开"记分会把这两类混为一谈，而它们的含义正好相反。
    """
    import httpx

    # 必须带浏览器 UA：实测 pay.ldxp.cn 对默认的 python-httpx UA 返回 403，
    # 换成浏览器 UA 才返回真正的「链动小铺」首页。不带 UA 会把正规平台误判成挂了。
    UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    out = {}
    for host in sorted(hosts):
        rec = {"host": host}
        try:
            with httpx.Client(
                timeout=12, follow_redirects=True, headers={"User-Agent": UA}
            ) as c:
                r = c.get(f"https://{host}/")
            body = r.text[:4000]
            mt = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
            title = re.sub(r"\s+", " ", mt.group(1)).strip()[:60] if mt else ""
            rec["status"] = r.status_code
            rec["title"] = title
            if "suspected phishing" in body.lower():
                rec["verdict"] = "cloudflare_phishing"
            elif r.status_code < 400:
                rec["verdict"] = "reachable"
            else:
                rec["verdict"] = f"http_{r.status_code}"
        except Exception as e:
            # 刻意不叫 "unreachable" —— 那是在对目标站点下结论，而这里发生的
            # 很可能是本机的事：这台机器走 TUN 透明代理，实测同一批域名早上
            # 还能取回 Cloudflare 的钓鱼拦截页，几小时后 curl 和 httpx 都拿不到了。
            # 探测失败不是证据，别让它反过来影响对卖家的判断。
            rec["verdict"] = "probe_failed"
            rec["error"] = type(e).__name__
        out[host] = rec
    return out


class FloodStop(Exception):
    """撞到 FloodWait 就整轮停下 —— 继续打只会把等待时间叠成几十小时。"""

    def __init__(self, seconds: int):
        self.seconds = seconds


async def scan(
    targets_path: Path, out_path: Path, msg_limit: int, delay: float, source_chats: list
) -> None:
    doc = json.loads(targets_path.read_text())
    targets = doc["targets"]
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("session 未授权，先跑 session_string_generator.py")

    # 语料群解析一次，供所有 User 型目标复用（每个目标各解析一次是十几倍的浪费）
    resolved_sources = []
    for c in source_chats:
        try:
            resolved_sources.append(await resolve_entity(c))
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

    # 市场 regime：全局状态，写进每一行，让快照自带上下文
    regime = await check_regime(source_chats)
    for r in rows:
        r["market_regime"] = regime["level"]
    mark = {0: "·", 1: "⚠️", 2: "🛑"}[regime["level"]]
    print(f"\n{mark} 市场状态 level={regime['level']} —— {regime['meaning']}")
    if regime["hits"]:
        print(f"   72h 内跨 {regime['distinct_chats']} 个群命中 {len(regime['hits'])} 条：")
        for h in regime["hits"][:3]:
            print(f"     [{h['word']}] {h['date']} {h['chat']}: {h['text'][:60]}")

    await client.disconnect()

    # 域名去重后统一探测：17 个目标各查各的会有大量重复请求
    hosts = {d for r in rows for d in (r.get("domains") or [])}
    if hosts:
        print(f"\n探测 {len(hosts)} 个卡网域名…")
        verdicts = classify_domains(hosts)
        phishing = []
        for r in rows:
            checks = [verdicts[d] for d in (r.get("domains") or []) if d in verdicts]
            if checks:
                r["domain_checks"] = checks
                bad = [c["host"] for c in checks if c["verdict"] == "cloudflare_phishing"]
                if bad:
                    r["phishing_domains"] = bad
                    phishing.append((r["username"], bad))
        for host, v in sorted(verdicts.items()):
            mark = {"cloudflare_phishing": "🚩钓鱼", "reachable": "·", "probe_failed": "○"}.get(v["verdict"], "?")
            print(f"  {mark} {host:<34} {v['verdict']:<20} {v.get('title', '')[:30]}")
        if phishing:
            print("\n  ⚠️ 以下对象的卡网被 Cloudflare 判定为钓鱼：")
            for u, bad in phishing:
                print(f"     @{u} → {', '.join(bad)}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    gone = sum(1 for r in rows if r["status"] == "username_gone")
    print(f"\n写入 {out_path}")
    print(f"共 {len(rows)} 条：存活 {sum(1 for r in rows if r['alive'])}，消失 {gone}")
    if stopped:
        print(f"⚠️ 本轮因 FloodWait({stopped}s) 提前结束，剩余目标未检查")


# 论坛转发频道的固定格式，例如「forum_user (@FORUM_USER) 在 <标题> 中发帖」
RELAY_RE = re.compile(r"@[A-Za-z0-9_]{3,32}\)?\s*在\s*.{0,80}?中发帖", re.S)
# 提及与关键词相隔多少字符以内才算"可能在说他"
NEAR_CHARS = 25



async def scan_corpus(
    out_path: Path, source_chats: list, per_word: int, since_days: int, delay: float
) -> None:
    """corpus-first：搜负面词拿回语料，再从语料里反查被指认的 handle。

    为什么不能反过来（handle-first）：实测搜 "demo_seller1" 在他刷屏最多的群里返回 0 条 ——
    那 332 条是他自己发的广告，而广告里不含自己的 handle。全文检索索引的是正文，
    所以"搜卖家名字数负面词"这个直觉做法，对数据主体一律返回空。
    反过来搜"骗子"，被指认的人自然会出现在正文里（"@xxx 这是骗子，大家不要上当"）。
    """
    from collections import Counter, defaultdict

    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("session 未授权")

    cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400
    chats = []
    for c in source_chats:
        try:
            chats.append((c, await resolve_entity(c)))
        except Exception as e:
            print(f"  ! {c} 解析失败: {type(e).__name__}")
    print(f"语料群 {len(chats)}/{len(source_chats)} 可用，负面词 {len(NEGATIVE_WORDS)} 个\n")

    records, accused = [], defaultdict(Counter)
    for name, chat in chats:
        for word in NEGATIVE_WORDS:
            n = 0
            try:
                async for msg in client.iter_messages(chat, search=word, limit=per_word):
                    if not msg.date or msg.date.timestamp() < cutoff:
                        continue
                    text = msg.message or ""
                    if RELAY_RE.search(text):
                        # 论坛转发频道的格式是「@作者 在 <标题> 中发帖」。这里的 @ 是
                        # 发帖人，不是被指认者；而关键词往往命中的是标题里的正常讨论
                        # （"Gemini 也不是一无是处…又快又稳"、"eSIM 洋垃圾"）。
                        # 不剔除的话，高产网友会排到疑似骗子榜首 —— 实测就是如此。
                        continue
                    mentions = {
                        x.casefold() for x in re.findall(r"@([A-Za-z0-9_]{5,32})", text)
                    }
                    # 只有和关键词靠得近的提及才可能是"被指认的人"。远处的 @ 多半无关。
                    near = set()
                    kpos = [mm.start() for mm in re.finditer(re.escape(word), text)]
                    for mth in re.finditer(r"@([A-Za-z0-9_]{5,32})", text):
                        if any(abs(mth.start() - k) <= NEAR_CHARS for k in kpos):
                            near.add(mth.group(1).casefold())
                    records.append({
                        "keyword": word,
                        "chat": name,
                        "message_id": msg.id,
                        "date": msg.date.strftime("%Y-%m-%d"),
                        "sender_id": msg.sender_id,
                        "mentions": sorted(mentions),
                        "near_mentions": sorted(near),
                        "text": text[:400],
                    })
                    # 只把"近距离共现"计入嫌疑，且仍需人工/模型判读：实测近距离里
                    # 也混着"@你 买什么被骗了"（问受害者）和"冒充号，认准 @我"（卖家澄清）。
                    for h in near:
                        accused[h][word] += 1
                    n += 1
            except errors.FloodWaitError as e:
                print(f"  !! FloodWait {e.seconds}s，停止")
                raise FloodStop(e.seconds) from e
            except Exception:
                continue
            if n:
                print(f"  {name:<22} {word:<6} {n:>3} 条")
            await asyncio.sleep(delay)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_chats": [c for c, _ in chats],
        "negative_words": NEGATIVE_WORDS,
        "since_days": since_days,
        "records": records,
        "accused": {
            h: {"total": sum(c.values()), "by_word": dict(c)}
            for h, c in sorted(accused.items(), key=lambda x: -sum(x[1].values()))
        },
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    await client.disconnect()
    print(f"\n写入 {out_path}")
    print(f"负面语料 {len(records)} 条，涉及 {len(accused)} 个被提及的 handle")


async def dump_urls(out_path: Path, source_chats: list, since_days: int, delay: float) -> None:
    """把带链接的消息按全文抓下来，输出成 sockpuppet.py 能直接吃的语料。

    为什么只抓带链接的：卡网、收款页、店铺路径全在这类消息里，而它们是马甲聚类
    最硬的信号来源。实测 7 个交易群总共只有 2829 条带链接的消息，而消息总量十几万
    —— 服务端过滤（InputMessagesFilterUrl）让这件事从"不可能"变成"半分钟"。

    为什么必须存全文：4-29 那次只存了 text_snippet，中位长度 68 字符，收款地址和
    卡网链接大多被截断。这直接封死了马甲聚类的召回率上限。
    """
    from telethon.tl import types as tl_types

    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("session 未授权")

    cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400
    records, capped = [], []
    for name in source_chats:
        try:
            ent = await resolve_entity(name)
        except Exception as e:
            print(f"  ! {name} 解析失败: {type(e).__name__}")
            continue
        n = 0
        try:
            async for msg in client.iter_messages(
                ent, filter=tl_types.InputMessagesFilterUrl()
            ):
                if not msg.date or msg.date.timestamp() < cutoff:
                    break  # 服务端按时间倒序返回，早于窗口即可停
                text = msg.message or ""
                sender = msg.sender
                records.append({
                    "chat": {"username": name, "peer_id": getattr(ent, "id", None)},
                    "message_id": msg.id,
                    "date": msg.date.isoformat(),
                    "sender": {
                        "peer_id": msg.sender_id,
                        "username": getattr(sender, "username", None),
                        "title": getattr(sender, "title", None)
                        or getattr(sender, "first_name", None),
                    },
                    # 字段名与 4-29 快照对齐，sockpuppet.py 可直接消费
                    "text_snippet": text,          # 全文，不截断
                    "external_urls": re.findall(r"https?://[^\s<>\"']+", text),
                    "telegram_handles": re.findall(r"@([A-Za-z0-9_]{5,32})", text),
                    "prices": _prices(text),
                })
                n += 1
        except errors.FloodWaitError as e:
            print(f"  !! FloodWait {e.seconds}s，停止")
            raise FloodStop(e.seconds) from e
        except Exception as e:
            print(f"  ! {name} 抓取中断: {type(e).__name__}")
        print(f"  {name:<20} {n:>5} 条带链接消息")
        await asyncio.sleep(delay)

    named = sum(1 for r in records if r["sender"]["username"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "带链接消息的全文语料（filter=InputMessagesFilterUrl）。字段与 4-29 快照对齐。",
        "since_days": since_days,
        "source_chats": source_chats,
        "sale_records_count": len(records),
        "sale_records": records,
    }, ensure_ascii=False, indent=1))
    await client.disconnect()
    print(f"\n写入 {out_path}")
    print(f"共 {len(records)} 条，其中 {named} 条能拿到发送者 username")
    lens = sorted(len(r["text_snippet"]) for r in records) or [0]
    print(f"正文长度中位 {lens[len(lens)//2]} 字符（4-29 那份是 68）")


DISCLAIMER = (
    "只做相对可核查度分层，不构成购买背书。这个市场天然有封号、掉订阅、不到账、"
    "售后失联的风险，且多数商业模式违反官方条款。任何档位都不代表安全。"
)
TIER_DEF = {
    "A": "近期活跃 + 报价明确 + 卡网可访问 + 无负面命中",
    "B": "活跃或频次高，但证据单一 / 报价不透明 / 商品风险更高",
    "C": "证据不足，或长期沉寂，或商业模式风险高",
    "D": "已消失 / 痕迹被抹除 / 卡网被判钓鱼 / 与被指认的骗子同组",
}


def build_report(snapshot: Path, negative: Path, clusters: Path,
                 frequency: Path, prices: Path) -> dict:
    """把各路产物合成一份分档结论。

    固化结论的形状是有原因的：此前每次调研都从零重新发明报告结构，
    于是两份报告无法比对、也无法复算 —— 直接堵死了"三个月后再查一次"。

    评分规则全部写死、可复算，不调参。输出永远带 evidence 和 disclaimer：
    没有原文出处的判断不该出现在结论里。
    """
    rows = [json.loads(x) for x in snapshot.read_text().splitlines() if x.strip()]
    acc = json.loads(negative.read_text()).get("accused", {}) if negative else {}
    cl = json.loads(clusters.read_text()).get("clusters", []) if clusters else []
    freq = {r["handle"]: r for r in
            (json.loads(frequency.read_text()).get("frequency", []) if frequency else [])}
    pr = json.loads(prices.read_text()) if prices else {}
    trans, outliers = pr.get("price_transparency", {}), pr.get("outliers", [])

    # handle → 同组的其他号
    group_of = {}
    for c in cl:
        for h in c["members"]:
            group_of[h] = c
    out_by_seller = defaultdict(list)
    for o in outliers:
        out_by_seller[o.get("seller", "")].append(o)

    regime = max((r.get("market_regime") or 0) for r in rows) if rows else 0
    results = []
    for r in rows:
        u = r["username"].lower()
        flags, ev = [], []

        if not r.get("alive"):
            flags.append("username 已消失")
            ev.append({"dimension": "存活", "quote": r.get("status"), "observed_at": r["checked_at"][:10]})
        if r.get("history_wiped"):
            flags.append(f"历史帖被整片删除（抽查 {r.get('past_posts_checked')} 条全不在）")
            ev.append({"dimension": "痕迹", "quote": "past_posts_alive=0", "observed_at": r["checked_at"][:10]})
        if r.get("phishing_domains"):
            flags.append(f"卡网被 Cloudflare 判定为钓鱼：{', '.join(r['phishing_domains'])}")
        for f in ("scam", "fake", "restricted"):
            if r.get(f):
                flags.append(f"官方标记 {f}")

        hit = acc.get(u)
        if hit:
            flags.append(f"群内被指认 {hit['total']} 次：{list(hit['by_word'])}")
            ev.append({"dimension": "负面", "quote": str(hit["by_word"]), "observed_at": "见负面语料"})

        grp = group_of.get(u)
        mates = [x for x in (grp["members"] if grp else []) if x != u]
        accused_mates = [x for x in mates if x in acc]
        if accused_mates:
            flags.append(f"与被指认者同组：{', '.join('@' + x for x in accused_mates)}")

        fq = freq.get(u, {})
        spam = fq.get("spam_ratio")
        if spam and spam >= 5:
            flags.append(f"活跃度靠刷屏：{fq['raw']} 条只有 {fq['deduped']} 条真实文案（{spam} 倍）")

        # 报价透明度：语料按 sender 索引，而频道自己发的帖不在里面 —— 那是数据缺口，
        # 不是"不透明"。缺口用快照自己采的样本补：它已经对每个对象抓了 30 条消息。
        # 这个区分很要紧：把"没数据"当成"0 分"，会把还在正常报价的频道一起打下去。
        tp = trans.get(u, {}).get("price_transparency")
        tp_src = "corpus" if tp is not None else None
        if tp is None and r.get("msgs_sampled"):
            tp = round(min(len(r.get("prices") or []) / 5.0, 1.0), 2)
            tp_src = "snapshot"
        if tp == 0:
            n = trans.get(u, {}).get("msgs") or r.get("msgs_sampled")
            flags.append(f"从不公开报价（{n} 条消息 0 条带价）")

        if out_by_seller.get(u):
            o = out_by_seller[u][0]
            flags.append(f"报价低到不可能：{o['category']} ¥{o['cny']}（同品类中位见基线）")

        days = r.get("days_since_last_msg")
        if days is not None and days > 60:
            flags.append(f"沉寂 {days} 天")

        # 分档：D 是一票否决项，其余按证据强度递降
        hard = any(k in " ".join(flags) for k in
                   ("已消失", "整片删除", "钓鱼", "被指认", "同组", "官方标记"))
        if hard:
            tier = "D"
        elif not r.get("alive"):
            tier = "D"
        elif (spam and spam >= 5) or tp == 0 or (days or 0) > 60 or out_by_seller.get(u):
            tier = "C"
        elif days is not None and days <= 14 and tp:
            # A 档要求同时满足：近两周还在、确实公开报价、且上面一条红旗都没有。
            # 注意 tp 为 None（既无语料也无快照样本）不算满足 —— 但也不扣分，
            # 只是拿不到 A，并在 confidence 里标出来。
            tier = "A"
        else:
            tier = "B"

        # 市场级冻结：全市场掉链子时 A 档一律降 B
        if regime >= 2 and tier == "A":
            tier = "B"
            flags.append("市场级事件进行中，A 档强制降档")

        results.append({
            "handle": r["username"], "tier": tier, "tier_definition": TIER_DEF[tier],
            "tier_20260429": r.get("tier_20260429"),
            "dimensions": {
                "alive": r.get("alive"), "recency_days": days,
                "history_wiped": bool(r.get("history_wiped")),
                "spam_ratio": spam, "price_transparency": tp,
                "price_transparency_source": tp_src,
                "negative_hits": (hit or {}).get("total", 0),
                "aliases": mates, "domains": r.get("domains") or [],
            },
            "red_flags": flags, "evidence": ev,
            "market_regime": regime,
            # 证据齐 → high；关键维度缺数据 → low，别让读者把"没查到"读成"没问题"
            "confidence": ("high" if ev else "medium") if tp is not None else "low",
            "disclaimer": DISCLAIMER,
        })

    order = {"A": 0, "B": 1, "C": 2, "D": 3}
    results.sort(key=lambda x: (order[x["tier"]], x["handle"].lower()))
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "market_regime": regime, "disclaimer": DISCLAIMER, "sellers": results}


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
    ap.add_argument("--scan-corpus", action="store_true", help="搜负面词反查被指认的 handle")
    ap.add_argument("--per-word", type=int, default=40, help="每个词每群最多取多少条（默认 40）")
    ap.add_argument("--since-days", type=int, default=120, help="只看最近多少天（默认 120）")
    ap.add_argument("--dump-urls", action="store_true", help="抓带链接消息的全文语料")
    ap.add_argument("--prices", nargs="+", metavar="语料", help="按品类算价格基线并标出离群低价")
    ap.add_argument("--report", action="store_true", help="合成分档结论（需 --snapshot 等输入）")
    for a in ("snapshot", "negative-in", "clusters-in", "frequency-in", "prices-in"):
        ap.add_argument(f"--{a}", type=Path)
    args = ap.parse_args()

    sources_ = [c.strip() for c in args.source_chats.split(",") if c.strip()]
    if args.report:
        rep = build_report(args.snapshot, args.negative_in, args.clusters_in,
                           args.frequency_in, args.prices_in)
        cur = None
        for s_ in rep["sellers"]:
            if s_["tier"] != cur:
                cur = s_["tier"]
                print(f"\n━━ {cur} 档 —— {s_['tier_definition']}")
            was = s_.get("tier_20260429")
            move = "" if was == s_["tier"] else f"  （4-29 是 {was} 档）"
            print(f"  @{s_['handle']}{move}")
            for f in s_["red_flags"][:4]:
                print(f"      · {f}")
        if args.out:
            args.out.write_text(json.dumps(rep, ensure_ascii=False, indent=1))
            print(f"\n写入 {args.out}")
        return
    if args.prices:
        rep = price_report(args.prices)
        print(f"{'品类':<14}{'样本':>6}{'中位(¥)':>10}{'MAD':>8}{'低价红线':>10}")
        print("-" * 50)
        for cat, st in sorted(rep["category_stats"].items(), key=lambda x: -x[1]["n"]):
            print(f"{cat:<14}{st['n']:>6}{st['median']:>10}{st['mad']:>8}{st['floor']:>10}")
        print(f"\n低于红线的报价 {len(rep['outliers'])} 条（低到不可能，需复核）：")
        for o in rep["outliers"][:10]:
            print(f"  {o['category']:<12} ¥{o['cny']:<8} @{o['seller'] or '?'} {o['date']}  「{o['raw']}」")
        print("\n报价透明度最低的卖家（不在群里报价 ≠ 数据缺失，是该扣分的地方）：")
        low = sorted(rep["price_transparency"].items(), key=lambda x: x[1]["price_transparency"])
        for u, v in low[:8]:
            print(f"  @{u:<24} {v['with_price']:>3}/{v['msgs']:<4} = {v['price_transparency']}")
        if args.out:
            args.out.write_text(json.dumps(rep, ensure_ascii=False, indent=1))
            print(f"\n写入 {args.out}")
        return
    if args.dump_urls:
        if not args.out:
            ap.error("--dump-urls 需要 --out")
        try:
            asyncio.run(dump_urls(args.out, sources_, args.since_days, args.delay))
        except FloodStop as fs:
            sys.exit(f"因 FloodWait({fs.seconds}s) 中止")
    elif args.scan_corpus:
        if not args.out:
            ap.error("--scan-corpus 需要 --out")
        try:
            asyncio.run(
                scan_corpus(args.out, sources_, args.per_word, args.since_days, args.delay)
            )
        except FloodStop as fs:
            sys.exit(f"因 FloodWait({fs.seconds}s) 中止")
    elif args.diff:
        diff(*args.diff)
    elif args.targets and args.out:
        sources = sources_
        asyncio.run(scan(args.targets, args.out, args.msg_limit, args.delay, sources))
    else:
        ap.error("要么给 --targets 和 --out，要么给 --diff")


if __name__ == "__main__":
    main()
