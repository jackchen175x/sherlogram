"""骗子库：一人一行的名单文件（JSONL），按改不掉的数字 ID 记，用户名只当别名；没有证据不入库。

规矩抄自社区里跑得通的做法：只认数字 ID、每条证据带群 / 日期 / 消息号或举报人、合并只追加不删、
档位取最重的、没证据的一律丢掉。导出成 Rose 群管机器人能直接导入的联盟封禁格式，群主一键封人。
本模块不连 Telegram，纯文件处理；工具在 trace.py 里。
"""

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

LIBRARY_NAME = "scamlist.jsonl"
_SEVERITY = {"A": 0, "B": 1, "C": 2, "D": 3}
_LIST_FIELDS = ("usernames", "names", "red_flags", "wallets", "domains", "related", "reporters")


def today() -> str:
    return f"{datetime.now(timezone.utc):%Y-%m-%d}"


def _ev_key(e: Dict[str, Any]) -> tuple:
    return (e.get("chat"), e.get("message_id"), (e.get("quote") or "")[:80])


def record_from_card(card: Dict[str, Any], reporter: str, note: Optional[str] = None) -> Dict[str, Any]:
    """把 check_seller 的卡片压成一条名单记录。没有数字 ID（用户名已不存在）或没有证据就抛 ValueError。"""
    profile = card.get("profile") or {}
    if not profile.get("id"):
        raise ValueError("没有数字 ID（账号已不存在），记不进去：用户名会换主人，只认 ID。")
    evidence = [dict(e) for e in card.get("evidence") or []]
    if note:
        evidence.append({"dimension": "人工", "date": today(), "quote": note.strip()[:400], "reporter": reporter})
    hard = [f for f in card.get("red_flags") or [] if any(k in f for k in ("钓鱼", "官方标记", "被指认"))]
    if not evidence and not hard:
        raise ValueError("没有证据不入库：卡片里没有原话、没有钓鱼 / 官方标记，也没给 note。")
    username = (card.get("handle") or "").lstrip("@").casefold()
    return {
        "user_id": profile["id"], "kind": profile.get("kind"),
        "usernames": [username] if username and not username.isdigit() else [],
        "names": [profile["name"]] if profile.get("name") else [],
        "tier": card.get("tier", "C"), "red_flags": list(card.get("red_flags") or []), "evidence": evidence,
        "wallets": list((card.get("clues") or {}).get("signals", {}).get("crypto", [])),
        "domains": list((card.get("clues") or {}).get("domains", [])),
        "related": [r["handle"] for r in card.get("related_accounts") or []],
        "reporters": [reporter], "first_reported": today(), "last_updated": today(), "source": "tg-mcp",
    }


def merge_records(records: Iterable[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """按数字 ID 合并：别名 / 证据取并集（证据按群+消息号去重）、档位取最重、时间取最早和最晚。没证据的丢。"""
    out: Dict[int, Dict[str, Any]] = {}
    for r in records:
        uid = r.get("user_id")
        if not isinstance(uid, int) or not r.get("evidence"):
            continue
        if uid not in out:
            out[uid] = {k: (list(v) if isinstance(v, list) else v) for k, v in r.items()}
            continue
        m = out[uid]
        for f in _LIST_FIELDS:
            m[f] = list(dict.fromkeys([*(m.get(f) or []), *(r.get(f) or [])]))
        seen = {_ev_key(e) for e in m["evidence"]}
        m["evidence"] += [e for e in r["evidence"] if _ev_key(e) not in seen]
        if _SEVERITY.get(r.get("tier"), 0) > _SEVERITY.get(m.get("tier"), 0):
            m["tier"] = r["tier"]
        m["first_reported"] = min(m.get("first_reported") or "9", r.get("first_reported") or "9")
        m["last_updated"] = max(m.get("last_updated") or "", r.get("last_updated") or "")
    return out


def load(path: Path) -> Dict[int, Dict[str, Any]]:
    if not path.exists():
        return {}
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 坏行跳过，别让一行烂数据锁死整个库
    return merge_records(rows)


def save(path: Path, lib: Dict[int, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for _, r in sorted(lib.items())), encoding="utf-8")


def find(lib: Dict[int, Dict[str, Any]], key: Any) -> List[Dict[str, Any]]:
    """按数字 ID、@用户名、收款地址或卡网域名查；用户名 / 域名不分大小写。"""
    if isinstance(key, int) or (isinstance(key, str) and key.lstrip("-").isdigit()):
        r = lib.get(int(key))
        return [r] if r else []
    k = str(key).lstrip("@").casefold()
    return [r for r in lib.values() if k in r.get("usernames", []) or k in [d.casefold() for d in r.get("domains", [])] or key in r.get("wallets", [])]


def summary(r: Dict[str, Any]) -> Dict[str, Any]:
    """给工具输出用的压缩版：别把整条证据都吐回去。"""
    return {
        "user_id": r["user_id"], "usernames": r.get("usernames", []), "names": r.get("names", []), "tier": r.get("tier"),
        "evidence_count": len(r.get("evidence", [])), "reporters": r.get("reporters", []),
        "first_reported": r.get("first_reported"), "last_updated": r.get("last_updated"),
        "red_flags": r.get("red_flags", [])[:4], "wallets": r.get("wallets", []), "domains": r.get("domains", []),
        "evidence": [{k: e.get(k) for k in ("dimension", "chat", "date", "quote") if e.get(k)} for e in r.get("evidence", [])[:3]],
    }


def export(lib: Dict[int, Dict[str, Any]], fmt: str = "rose_csv", min_tier: str = "D") -> str:
    """导出给别人用。rose_csv / rose_json 是 Rose 群管机器人联盟封禁的导入格式；json 是本库原样。"""
    rows = [r for _, r in sorted(lib.items()) if _SEVERITY.get(r.get("tier"), 0) >= _SEVERITY[min_tier]]
    if fmt == "json":
        return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)

    def reason(r):
        flag = (r.get("red_flags") or ["见证据"])[0]
        return f"{r.get('tier')}档：{flag}（{len(r.get('evidence', []))} 条证据，tg-mcp {r.get('last_updated')}）"[:200]

    if fmt == "rose_json":
        return "".join(
            json.dumps({"user_id": r["user_id"], "first_name": (r.get("names") or [""])[0], "last_name": "",
                        "user_name": (r.get("usernames") or [""])[0], "reason": reason(r)}, ensure_ascii=False) + "\n"
            for r in rows
        )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "reason", "firstname", "lastname", "username"])
    for r in rows:
        w.writerow([r["user_id"], reason(r), (r.get("names") or [""])[0], "", (r.get("usernames") or [""])[0]])
    return buf.getvalue()
