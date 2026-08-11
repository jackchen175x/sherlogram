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
            ent = await m.resolve_entity(name)
        except Exception:
            continue
        for word in REGIME_WORDS:
            try:
                async for msg in m.client.iter_messages(ent, search=word, limit=10):
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

    await m.client.disconnect()

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

# Jack 在《卖家靠谱度核查》里手工用的负面词，原样沿用以保持结论可比
NEGATIVE_WORDS = [
    "骗子", "被骗", "曝光", "跑路", "拉黑", "不稳", "掉订阅",
    "无质保", "不到账", "退款", "维权", "失联", "假的", "垃圾",
]


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

    await m.client.connect()
    if not await m.client.is_user_authorized():
        sys.exit("session 未授权")

    cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400
    chats = []
    for c in source_chats:
        try:
            chats.append((c, await m.resolve_entity(c)))
        except Exception as e:
            print(f"  ! {c} 解析失败: {type(e).__name__}")
    print(f"语料群 {len(chats)}/{len(source_chats)} 可用，负面词 {len(NEGATIVE_WORDS)} 个\n")

    records, accused = [], defaultdict(Counter)
    for name, chat in chats:
        for word in NEGATIVE_WORDS:
            n = 0
            try:
                async for msg in m.client.iter_messages(chat, search=word, limit=per_word):
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
    await m.client.disconnect()
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

    await m.client.connect()
    if not await m.client.is_user_authorized():
        sys.exit("session 未授权")

    cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400
    records, capped = [], []
    for name in source_chats:
        try:
            ent = await m.resolve_entity(name)
        except Exception as e:
            print(f"  ! {name} 解析失败: {type(e).__name__}")
            continue
        n = 0
        try:
            async for msg in m.client.iter_messages(
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
    await m.client.disconnect()
    print(f"\n写入 {out_path}")
    print(f"共 {len(records)} 条，其中 {named} 条能拿到发送者 username")
    lens = sorted(len(r["text_snippet"]) for r in records) or [0]
    print(f"正文长度中位 {lens[len(lens)//2]} 字符（4-29 那份是 68）")


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
    args = ap.parse_args()

    sources_ = [c.strip() for c in args.source_chats.split(",") if c.strip()]
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
