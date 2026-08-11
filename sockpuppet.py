#!/usr/bin/env python3
"""把同一个人的多个 Telegram 账号聚成一组。

为什么需要：骗子的核心手法就是换名字。被曝光 → 弃号 → 开新号 → 在同一个群继续卖。
只按 handle 追踪，换个马甲负面记录就归零，看起来是个"干净的新卖家"。
所以要改成按"他甩不掉的东西"追踪：收款地址、卡网 URL、报价单。

纯离线：只读已有的扫描语料，不发一个 Telegram 请求，因此零封号风险、可随便重跑。

    uv run python3 sockpuppet.py --corpus <4-29快照.json> [--corpus <更多.json>] \
        --negative <负面语料.json> --out <结果.json>

聚类规则刻意笨：并查集 + 「一条强信号，或两条弱信号」。不上模型、阈值写死。
输出的是证据，不是判决 —— 第一版必然有误报，让人看证据再定。
"""
import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

# 强信号：几乎不可能是巧合
CRYPTO_RE = re.compile(r"\b(?:T[A-Za-z0-9]{33}|0x[a-fA-F0-9]{40}|bc1[a-z0-9]{25,62})\b")
WECHAT_RE = re.compile(r"(?:微信|weixin|wechat|vx|VX)\s*[号:：]?\s*([A-Za-z][\w\-]{5,19})")
EMAIL_RE = re.compile(r"\b[\w.\-]+@[\w\-]+\.[a-z]{2,}\b", re.I)

# 群友明说"这俩是同一个人"的句式 —— 目前最硬的证据，胜过任何统计特征。
# 例："他把用户名从 @old_handle_1 改成了 @new_handle_1"、"@main_handle ... 小号@alt_handle"
ALIAS_PATTERNS = [
    re.compile(r"从\s*@?([A-Za-z0-9_]{5,32})\s*改(?:成|为)了?\s*@?([A-Za-z0-9_]{5,32})"),
    re.compile(r"@([A-Za-z0-9_]{5,32}).{0,40}?小号\s*@?([A-Za-z0-9_]{5,32})"),
    re.compile(r"@([A-Za-z0-9_]{5,32}).{0,30}?(?:马甲|换号|曾用名|又叫|新号)\s*@?([A-Za-z0-9_]{5,32})"),
]
# 真正的曝光帖（而不是"碰巧含负面词的自家广告"）才有资格贡献共现信号
EXPOSURE_RE = re.compile(
    r"(曝光|谨防受骗|被骗提醒|踩个坑|是骗子|这是骗子|注意拉黑|大家不要上当|付款后不发货|收了U不发货)"
)

# 太通用、串起来没有意义的域名（社交平台、官网、短链）
GENERIC_HOSTS = {
    "t.me", "telegram.org", "telegram.me", "x.com", "twitter.com", "github.com",
    "openai.com", "chatgpt.com", "anthropic.com", "claude.ai", "google.com",
    "youtube.com", "bit.ly", "linux.do", "docs.google.com", "gemini.google.com",
}


class Union:
    """并查集。用它是因为「A 和 B 是一伙、B 和 C 是一伙」必须自动推出 A 和 C 同组。"""

    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def norm_url(u: str) -> str:
    """归一到「主机 + 路径」。保留 /shop/<名字> —— 卡网是多租户的，
    同一个平台上不同店铺是不同的人，按域名株连会把正规平台整个拉黑
    （实测教训：pay.ldxp.cn 是正规的链动小铺，被仿冒的是 play.ldxp.demo-fake.cn）。"""
    u = u.strip().rstrip(".,;)]}】》")
    m = re.match(r"https?://([^/\s]+)(/[^\s?#]*)?", u, re.I)
    if not m:
        return ""
    host = m.group(1).lower().lstrip("www.")
    if host in GENERIC_HOSTS:
        return ""
    path = (m.group(2) or "").rstrip("/")
    return host + path


def norm_text(t: str) -> str:
    """文案指纹：把规避检索用的混淆字符彻底抹平，只留可比的骨架。

    卖家把零宽字符和间隔号插进单词中间来躲检索，例如实际文本长这样：
        G·em·i‌n‍i  ‌P﻿r‌o        （中间夹着 U+200C / U+200D / U+1808 和「·」）

    关键是**删除**而不是替换成空格：分隔符的位置每条都不一样，替换成空格后
    「G·em·ini」和「Ge·mi·ni」仍是两个不同的字符串，等于没归一。
    实测差别极大 —— @demo_seller1 的 332 条记录，替换式只能去重到 307 条，
    删除式去重到 11 条（真实模板其实只有三四个，他连字母都随机漏，
    shopname_kyc / shopname_ky / shopname_kc 各算一条）。
    """
    t = unicodedata.normalize("NFKC", t)
    # Cf = 格式字符（零宽连接符等），Mn = 组合记号，都是常见的隐形分隔手段
    t = "".join(c for c in t if unicodedata.category(c) not in ("Cf", "Mn"))
    return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", t).lower()


def collect(corpus_paths, negative_path):
    """从各份语料里抽出 handle → 各类身份信号。"""
    sig = defaultdict(lambda: defaultdict(set))  # handle -> kind -> {values}
    seen_text = defaultdict(set)

    for p in corpus_paths:
        d = json.loads(Path(p).read_text())
        for r in d.get("sale_records", []):
            u = ((r.get("sender") or {}).get("username") or "").lower()
            if not u:
                continue
            for url in r.get("external_urls") or []:
                n = norm_url(url)
                if n:
                    sig[u]["url"].add(n)
            text = r.get("text_snippet") or ""
            for kind, rx in (("crypto", CRYPTO_RE), ("email", EMAIL_RE)):
                sig[u][kind].update(rx.findall(text))
            for w in WECHAT_RE.findall(text):
                sig[u]["wechat"].add(w.lower())
            nt = norm_text(text)
            if len(nt) >= 40:  # 太短的文案撞车没有意义
                seen_text[u].add(nt)
        # 报价单指纹：top_sellers 里带整套报价，逐字相同的报价单是很强的同源信号
        for s in (d.get("aggregates") or {}).get("top_sellers", []):
            u = (s.get("username") or "").lower()
            prices = [p for p in (s.get("prices") or []) if re.search(r"\d", p)]
            if u and len(prices) >= 3:
                sig[u]["pricelist"].add("|".join(sorted(set(prices))[:12]))

    if negative_path:
        d = json.loads(Path(negative_path).read_text())
        for r in d.get("records", []):
            text = r.get("text") or ""
            mid = r.get("message_id")

            # 1) 群友明说改名/小号 —— 直接连边，不需要其它佐证
            for rx in ALIAS_PATTERNS:
                for a, b in rx.findall(text):
                    tag = f"msg{mid}:{a.lower()}={b.lower()}"
                    sig[a.lower()]["rename"].add(tag)
                    sig[b.lower()]["rename"].add(tag)

            # 2) 只有真曝光帖才贡献共现与地址。否则会把卖家自家广告算进来 ——
            #    实测「电商客户退款不压资金」「概不退款」这类正常商品说明会命中负面词，
            #    把商家和自家机器人当成"共同被曝光"。
            if not EXPOSURE_RE.search(text):
                continue
            # 曝光帖的形式常是「这些 ID 是一伙的，共用这个地址」，一整串 ID 都要，
            # 不能只取靠近关键词的那几个（那样会漏掉列表末尾的马甲）。
            handles = [h.lower() for h in (r.get("mentions") or [])]
            if len(handles) < 2 or len(handles) > 10:
                continue
            addrs = set(CRYPTO_RE.findall(text))
            urls = {norm_url(u) for u in re.findall(r"https?://[^\s]+", text)}
            urls |= {norm_url("http://" + h) for h in re.findall(r"\b(?:[\w\-]+\.)+[a-z]{2,}(?:/\S*)?", text)}
            urls.discard("")
            for h in handles:
                sig[h]["crypto"].update(addrs)
                sig[h]["coexposed"].add(f"msg{mid}")
                sig[h]["url"].update(urls)

    for u, texts in seen_text.items():
        sig[u]["textfp"].update(list(texts)[:200])
    return sig


# coexposed 不在其中：被同一条曝光帖点名，也可能是几个各自独立的骗子，
# 或者受害者顺手 @ 的旁观者。它只能当佐证，不能单独定案。
STRONG = {"crypto", "wechat", "email", "pricelist", "rename"}


def name_kin(a: str, b: str) -> bool:
    """handle 是否像同一个人起的：去掉尾部数字和 bot/官方等后缀后相同。
    例 main_handle / main_handle77、catshop / catshop666、somebrand / somebrand_bot。"""
    def stem(x):
        return re.sub(r"(?:_?bot|官方|admin|[0-9]+)+$", "", x)
    sa, sb = stem(a), stem(b)
    return len(sa) >= 4 and sa == sb and a != b


def cluster(sig):
    """共享同一个信号值的 handle 连边。强信号一条即可，弱信号（url/文案）要两条。"""
    by_value = defaultdict(lambda: defaultdict(set))
    for h, kinds in sig.items():
        for kind, vals in kinds.items():
            for v in vals:
                by_value[kind][v].add(h)

    edges = defaultdict(lambda: defaultdict(set))  # (a,b) -> kind -> {values}
    for kind, vmap in by_value.items():
        for v, hs in vmap.items():
            if len(hs) < 2 or len(hs) > 8:
                continue  # 超过 8 个人共用的多半是公共平台，不是同一个人
            hl = sorted(hs)
            for i in range(len(hl)):
                for j in range(i + 1, len(hl)):
                    edges[(hl[i], hl[j])][kind].add(v)

    # 名字同源本身太弱（撞名很常见），但能把"只有一条弱信号"的候选顶到成案门槛
    for pair in list(edges):
        if name_kin(*pair):
            edges[pair]["namekin"].add(f"{pair[0]}~{pair[1]}")

    uf = Union()
    kept = {}
    for pair, kinds in edges.items():
        strong = [k for k in kinds if k in STRONG]
        if strong or len(kinds) >= 2:
            uf.union(*pair)
            kept[pair] = kinds

    groups = defaultdict(set)
    for pair in kept:
        for h in pair:
            groups[uf.find(h)].add(h)
    # 差一条证据就成案的配对：必须报出来，不能静默丢弃 ——
    # 否则读者会把"聚出 9 组"当成"就这 9 组"。
    near_miss = {p: k for p, k in edges.items() if p not in kept}
    return groups, kept, near_miss


def frequency_report(corpus_paths) -> list:
    """修正「出现频次」这一维：它现在衡量的是刷屏勤奋度，不是市场存在感。

    实测归一化去重后：demo_seller1 332 → 23 条真实文案，demo_seller2 89 → 5，
    demo_seller3 104 → 9。分档时按原始条数排序，等于奖励刷屏最狠的那个。

    归一化必须做，因为卖家会插零宽字符规避检索（@demo_seller1 的文案里就有 U+200C）——
    不去这些字符，同一条文案会被当成几十条不同的。
    """
    raw, uniq = Counter(), defaultdict(set)
    for p in corpus_paths:
        d = json.loads(Path(p).read_text())
        for r in d.get("sale_records", []):
            u = ((r.get("sender") or {}).get("username") or "").lower()
            if not u:
                continue
            raw[u] += 1
            nt = norm_text(r.get("text_snippet") or "")
            if nt:
                uniq[u].add(nt)

    rows = []
    for u, n in raw.most_common():
        k = len(uniq[u]) or 1
        rows.append({
            "handle": u, "raw": n, "deduped": len(uniq[u]),
            "spam_ratio": round(n / k, 1),
        })
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", action="append", required=True, help="扫描快照 JSON（可多次）")
    ap.add_argument("--negative", help="负面语料 JSON")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--frequency", action="store_true",
                    help="只输出「出现频次」修正表：原始条数 vs 归一化去重后")
    args = ap.parse_args()

    if args.frequency:
        rows = frequency_report(args.corpus)
        print(f"{'handle':<24}{'原始':>7}{'去重':>7}{'刷屏倍数':>10}")
        print("-" * 50)
        for r in rows[:25]:
            flag = "  ← 主要靠刷屏" if r["spam_ratio"] >= 5 else ""
            print(f"{r['handle']:<24}{r['raw']:>7}{r['deduped']:>7}{r['spam_ratio']:>10}{flag}")
        args.out.write_text(json.dumps({"frequency": rows}, ensure_ascii=False, indent=1))
        print(f"\n写入 {args.out}")
        return

    sig = collect(args.corpus, args.negative)
    groups, kept, near_miss = cluster(sig)
    print(f"共 {len(sig)} 个 handle 带信号，聚出 {len(groups)} 组\n")

    out = []
    for root, members in sorted(groups.items(), key=lambda x: -len(x[1])):
        ev = defaultdict(lambda: defaultdict(set))
        for (a, b), kinds in kept.items():
            if a in members and b in members:
                for k, vs in kinds.items():
                    ev[k][f"{a}+{b}"].update(vs)
        strength = "强" if any(k in STRONG for k in ev) else "中"
        rec = {
            "members": sorted(members),
            "size": len(members),
            "strength": strength,
            "evidence": {k: {p: sorted(v)[:4] for p, v in d.items()} for k, d in ev.items()},
        }
        out.append(rec)
        print(f"[{strength}] {len(members)} 个号: {', '.join('@' + x for x in sorted(members))}")
        for k, d in ev.items():
            for pair, vs in list(d.items())[:2]:
                print(f"      {k}: {sorted(vs)[0][:72]}")
    nm = [
        {"pair": list(p), "signals": {k: sorted(v)[:3] for k, v in kinds.items()}}
        for p, kinds in near_miss.items()
    ]
    print(f"\n=== 差一条证据的候选（{len(nm)} 对，未成案，需人工看） ===")
    for r in nm[:12]:
        kinds = ", ".join(f"{k}={sorted(v)[0][:44]}" for k, v in r["signals"].items())
        print(f"  @{r['pair'][0]} + @{r['pair'][1]}  ({kinds})")
    if len(nm) > 12:
        print(f"  …另有 {len(nm)-12} 对，见输出文件")
    args.out.write_text(json.dumps({"clusters": out, "near_miss": nm}, ensure_ascii=False, indent=1))
    print(f"\n写入 {args.out}")
    print("\n注意：这是证据不是判决。共用卡网 URL 也可能是同平台不同店铺，需人工确认。")


if __name__ == "__main__":
    main()
