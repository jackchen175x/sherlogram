"""各工具模块共用的小件：报错、编号校验、找聊天、格式化输出、文件路径白名单。"""

import asyncio
import json
from collections import Counter
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import unquote, urlparse

from mcp.server.fastmcp import Context
from mcp.shared.exceptions import McpError
from telethon import utils
from telethon.tl.types import Channel, Chat, User

from . import app
from .app import client, logger

ChatRef = Union[int, str]  # 数字编号或 @用户名


class ValidationError(Exception):
    """参数不对。在工具的 try 里直接 raise，兜底的 except 会把原话原样带给调用方。"""


class ToolError(RuntimeError):
    """log_and_format_error 抛出的错误。再经过一次 log_and_format_error 时原样放行，别再包一层。"""


def require(action: str, **params: Any) -> None:
    """带 action 的工具用：该动作必填的参数有空的就抛 ValidationError。"""
    missing = [k for k, v in params.items() if v in (None, "", [])]
    if missing:
        raise ValidationError(f"Action '{action}' requires: {', '.join(missing)}.")


# ---------- 报错 ----------


class ErrorCategory(str, Enum):
    CHAT = "CHAT"
    MSG = "MSG"
    CONTACT = "CONTACT"
    GROUP = "GROUP"
    MEDIA = "MEDIA"
    PROFILE = "PROFILE"
    AUTH = "AUTH"
    ADMIN = "ADMIN"
    FOLDER = "FOLDER"


def classify_error(e: Exception) -> str:
    """把 Telegram 的异常翻成一句人话；没有对应分类就返回空串。"""
    s, t = str(e), type(e).__name__
    if isinstance(e, ConnectionError) or "disconnected" in s.lower():
        return "Connection lost. The client will auto-reconnect on next request."
    if "FloodWaitError" in t or "flood" in s.lower():
        m = re.search(r"(\d+)", s)
        return f"Rate limited by Telegram. Please wait {m.group(1) if m else 'unknown'} seconds before retrying."
    if "AuthKeyError" in t or "Unauthorized" in t or "SESSION_REVOKED" in s:
        return "Session expired or revoked. Please regenerate your session string."
    if "ChatAdminRequiredError" in t:
        return "Admin privileges required for this operation."
    if "UserNotParticipantError" in t:
        return "You are not a member of this chat."
    if "PeerIdInvalidError" in t or "PeerIdInvalid" in s:
        return "Invalid chat/user ID. Please verify the ID is correct."
    if isinstance(e, asyncio.TimeoutError) or (isinstance(e, RuntimeError) and "timed out" in s):
        return "Request timed out. Telegram may be slow — please retry."
    return ""


def log_and_format_error(
    function_name: str,
    error: Exception,
    prefix: Optional[Union[ErrorCategory, str]] = None,
    user_message: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """记日志，然后把错误**以异常抛出**（返回类型只是为了让调用点能写 ``return ...``）。

    返回字符串时 MCP 协议层的 isError 是 False，agent 在批量调查里会把"连接断了"
    当成"这个卖家没有负面记录"一样的无害空结果略过 —— 对判断卖家可不可信的工具，
    这是最贵的失败模式。抛异常后 FastMCP 会置 isError=True 并保留原文。
    """
    if isinstance(error, ToolError):
        raise error  # 工具内部已经格式化过一次（比如 try 里校验失败），别再包成泛泛的 GEN-ERR
    if isinstance(error, ValidationError) and prefix is None:
        prefix, user_message = "VALIDATION-001", user_message or str(error)
    if prefix == "VALIDATION-001":
        code = prefix
    else:
        if prefix is None:
            prefix = next((c for c in ErrorCategory if c.name.lower() in function_name.lower()), None)
        prefix_str = prefix.value if isinstance(prefix, ErrorCategory) else (prefix or "GEN")
        # 不用 hash(function_name)：Python 的 str hash 每进程加盐，日志里的编号事后对不上
        code = f"{prefix_str}-ERR-{function_name}"
    context = ", ".join(f"{k}={v}" for k, v in kwargs.items())
    logger.error(f"Error in {function_name} ({context}) - Code: {code}", exc_info=True)
    if user_message:
        msg = user_message
    else:
        classified = classify_error(error)
        msg = f"{classified} (code: {code})" if classified else f"An error occurred (code: {code}). Check mcp_errors.log for details."
    raise ToolError(msg) from error


# ---------- 编号校验 ----------

_USERNAME_RE = re.compile(r"^@?[a-zA-Z0-9_]{5,}$")
_ID_MIN, _ID_MAX = -(2**63), 2**63 - 1


def _check_id(value: Any, name: str) -> Tuple[Any, Optional[str]]:
    """返回（规范化后的值, 错误信息）：整数或整数字符串 → int；用户名原样。"""
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            if value == "me" or _USERNAME_RE.match(value):  # "me" = 自己的「收藏夹」，Telethon 认
                return value, None
            return None, f"Invalid {name}: '{value}'. Must be a valid integer ID, or a username string."
    if not isinstance(value, int):
        return None, f"Invalid {name}: {value}. Type must be an integer or a string."
    if not _ID_MIN <= value <= _ID_MAX:
        return None, f"Invalid {name}: {value}. ID is out of the valid integer range."
    return value, None


def validate_id(*param_names: str):
    """装饰器：校验 chat_id / user_id 这类参数（单个或列表），整数字符串转成 int。"""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for name in param_names:
                value = kwargs.get(name)
                if value is None:
                    continue
                is_list = isinstance(value, list)
                checked = []
                for item in value if is_list else [value]:
                    ok, err = _check_id(item, name)
                    if err:
                        return log_and_format_error(
                            func.__name__, ValidationError(err), prefix="VALIDATION-001",
                            user_message=err, **{name: value},
                        )
                    checked.append(ok)
                kwargs[name] = checked if is_list else checked[0]
            return await func(*args, **kwargs)

        return wrapper

    return decorator


# ---------- 找聊天 ----------


async def ensure_connected(max_retries: int = 3, delay: float = 2.0) -> None:
    """确保 Telegram client 已连接，断连时自动重连。"""
    if client.is_connected():
        return
    for attempt in range(max_retries):
        try:
            logger.warning(f"Client disconnected, reconnecting (attempt {attempt + 1}/{max_retries})...")
            await client.connect()
            if client.is_connected():
                return
        except Exception as e:
            logger.error(f"Reconnect attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(delay * (attempt + 1))
    raise ConnectionError("Failed to reconnect to Telegram after all retries")


# 实体缓存：避免每次 resolve 失败都拉全量 dialogs。没有过期时间，scam/fake 这类标记可能是旧值。
# 用户名解析（ResolveUsername）会触发 FloodWait 限速，数字编号走缓存不会。
_entity_cache: Dict[ChatRef, Any] = {}
# 找不到就拉全部会话预热一次再找。预热很贵（几十个群要几秒），而一次列几十个陌生人的调用
# （比如分组详情）每个都失败就每个都预热——实测一次 30 秒。所以同一时间段只预热一次。
_WARM_INTERVAL = 600
_last_warm = 0.0


async def _warm_dialogs() -> bool:
    global _last_warm
    if time.monotonic() - _last_warm < _WARM_INTERVAL:
        return False
    await client.get_dialogs()
    _last_warm = time.monotonic()
    return True


async def resolve_entity(identifier: ChatRef) -> Any:
    """按编号或用户名找到群 / 频道 / 用户；缓存没有时拉一次 dialogs 预热再找。"""
    await ensure_connected()
    if identifier in _entity_cache:
        return _entity_cache[identifier]
    try:
        entity = await client.get_entity(identifier)
    except ValueError:
        if not await _warm_dialogs():
            raise
        entity = await client.get_entity(identifier)
    _entity_cache[identifier] = entity
    return entity


async def resolve_input_entity(identifier: ChatRef) -> Any:
    """同 resolve_entity，但返回 InputPeer（发请求用）。"""
    await ensure_connected()
    try:
        return await client.get_input_entity(identifier)
    except ValueError:
        if not await _warm_dialogs():
            raise
        return await client.get_input_entity(identifier)


# ---------- 格式化 ----------


def dump_json(obj: Any) -> str:
    """统一的 JSON 输出：中文不转义、不缩进、不带空格（每轮对话都在为字数付钱）。"""

    def default(o):
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, bytes):
            return o.decode("utf-8", errors="replace")
        raise TypeError(f"Object of type {type(o)} is not JSON serializable")

    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=default)


def parse_date(value: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    """'YYYY-MM-DD' → UTC datetime；end_of_day 取到当天最后一微秒。格式不对抛 ValueError。"""
    if not value:
        return None
    d = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return d + timedelta(days=1, microseconds=-1) if end_of_day else d


def get_entity_type(entity: Any) -> str:
    """人类可读的类型：User / Group (Basic) / Supergroup / Channel / Group。"""
    if isinstance(entity, User):
        return "User"
    if isinstance(entity, Chat):
        return "Group (Basic)"
    if isinstance(entity, Channel):
        if getattr(entity, "megagroup", False):
            return "Supergroup"
        return "Channel" if getattr(entity, "broadcast", False) else "Group"
    return type(entity).__name__


def get_entity_filter_type(entity: Any) -> Optional[str]:
    """list_chats 用的粗分类：user / group / channel。"""
    t = get_entity_type(entity)
    if t == "User":
        return "user"
    if t in ("Group (Basic)", "Group", "Supergroup"):
        return "group"
    if t == "Channel":
        return "channel"
    return None


def format_entity(entity: Any) -> Dict[str, Any]:
    """群 / 频道 / 用户的统一摘要：id、name、type、username、phone、官方标记。"""
    result: Dict[str, Any] = {"id": entity.id}
    if hasattr(entity, "title"):
        result["name"] = entity.title
        result["type"] = "group" if isinstance(entity, Chat) else "channel"
    elif hasattr(entity, "first_name"):
        result["name"] = " ".join(p for p in (entity.first_name, getattr(entity, "last_name", None)) if p)
        result["type"] = "user"
        if getattr(entity, "phone", None):
            result["phone"] = entity.phone
    # username 对 User/Chat/Channel 统一输出：拿不到 @username 就无法继续深挖
    if getattr(entity, "username", None):
        result["username"] = entity.username
    # 官方信任标记：高精度、低召回（大量骗子号 scam 仍为 False），只在为 True 时输出，
    # 避免模型把 False 读成"已核验安全"
    for flag in ("scam", "fake", "restricted", "verified"):
        if getattr(entity, flag, False):
            result[flag] = True
    return result


def format_message(message: Any) -> Dict[str, Any]:
    """一条消息的统一摘要：id、date、text、from_id、媒体类型。"""
    result: Dict[str, Any] = {"id": message.id, "date": message.date.isoformat(), "text": message.message or ""}
    if message.from_id:
        result["from_id"] = utils.get_peer_id(message.from_id)
    if message.media:
        result["has_media"] = True
        result["media_type"] = type(message.media).__name__
    return result


def get_sender_name(message: Any) -> str:
    """发消息的人：名字 (@用户名)；群 / 频道用标题。"""
    sender = message.sender
    if not sender:
        return "Unknown"
    if getattr(sender, "title", None):
        name = sender.title
    else:
        name = " ".join(p for p in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if p)
        if not name:
            return "Unknown"
    username = getattr(sender, "username", None)
    return f"{name} (@{username})" if username else name


def get_provenance_info(message: Any) -> str:
    """这条消息是怎么来的：转发自哪、原帖多久之前、是否被编辑过、是否机器人代发。

    三种手法此前完全不可见，而它们正是"刷屏复制文案但没有真实反馈"的具体形态：
      · 把三个月前的老帖 edit 成新价格 —— msg.date 还是旧的，只有 edit_date 会暴露
      · 转发一条 90 天前的老广告冒充今日现货 —— msg.date 是今天，fwd.date 才是真相
      · via_bot 说明是机器人批量代发，不是真人报价
    """
    parts = []
    fwd = getattr(message, "fwd_from", None)
    if fwd:
        if fwd.from_id:
            parts.append(f"fwd_from_id:{utils.get_peer_id(fwd.from_id)}")
        for attr in ("from_name", "channel_post", "post_author"):
            if getattr(fwd, attr, None):
                parts.append(f"fwd_{attr.replace('from_name', 'from_name').replace('post_author', 'author')}:{getattr(fwd, attr)}")
        if getattr(fwd, "date", None):
            parts.append(f"fwd_date:{fwd.date:%Y-%m-%d}")
            age = (message.date - fwd.date).days if getattr(message, "date", None) else 0
            if age >= 1:
                parts.append(f"fwd_age_days:{age}")
    if getattr(message, "edit_date", None):
        parts.append(f"edited:{message.edit_date:%Y-%m-%d}")
    if getattr(message, "via_bot_id", None):
        parts.append(f"via_bot:{message.via_bot_id}")
    return f" | provenance=[{', '.join(parts)}]" if parts else ""


def get_engagement_info(message: Any) -> str:
    """阅读 / 转发 / 表情反应的数量。"""
    parts = []
    for attr in ("views", "forwards"):
        if getattr(message, attr, None) is not None:
            parts.append(f"{attr}:{getattr(message, attr)}")
    reactions = getattr(message, "reactions", None)
    if reactions is not None:
        results = getattr(reactions, "results", None) or []
        parts.append(f"reactions:{sum(getattr(r, 'count', 0) or 0 for r in results)}")
    return f" | {', '.join(parts)}" if parts else ""


# ---------- 截断、线索抽取 ----------

MSG_CLIP = 400


def clip(text: str, full_text: bool = False) -> str:
    """长消息截断，但把截断这件事明说出来。

    注意：实测下来它的收益比预期小得多，别高估：
    limit=100 时全文 5681 token、截断后 5513 token，**只省 3%**。
    真正的成本大头是「消息条数 × 每行元数据」，不是长正文 —— 正文中位只有 61 字符
    （300 条样本：90 分位 174，最长 2310）。**想省上下文，调 limit 比截断有效得多。**

    那为什么还留着：偶发的 2000+ 字符刷屏广告确实存在，一页里撞上几条就很难看。
    这是兜底，不是优化手段。

    但被截掉的恰恰是信息最密的那些 —— 完整报价单、卡网链接、联系方式。所以：
      · 必须留 full_text 逃生口
      · 必须显式标出截了多少，否则就是静默截断，模型会把残缺当完整
      · 域名/@提及 的抽取在截断之前做（见 clues_tail），结构化信号不受影响
    """
    if full_text or not text or len(text) <= MSG_CLIP:
        return text
    return f"{text[:MSG_CLIP]}…[已截断，还有 {len(text) - MSG_CLIP} 字符，full_text=True 取全文]"


def clues_tail(messages) -> str:
    """把这批消息里高频出现的 @账号 / 域名 / 话题附在结果末尾。

    这是 corpus-first 的落地形态：调查的下一跳不该靠人再读一遍消息去捞，
    应该由上一次调用直接指出来。

    只输出出现 ≥2 次的，否则等于给每次调用平白加一段上下文。
    """
    clues = extract_public_post_clues([{"text": getattr(msg, "message", "") or ""} for msg in messages])
    parts = []
    for label, key in (("提及", "mentions"), ("域名", "linked_domains"), ("话题", "hashtags")):
        top = [x for x in clues[key] if x["count"] >= 2][:8]
        if top:
            parts.append(f"{label}: " + ", ".join(f"{x['value']}×{x['count']}" for x in top))
    if not parts:
        return ""
    return "\n\n--- 线索（本批消息里的高频项，可作为下一跳）---\n" + "\n".join(parts)


def extract_public_post_clues(posts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从一批帖子里抽出调查的下一跳：话题、@提及、外链域名、出现的频道。

    不带下划线：market_watch.py 等别的脚本也要用。
    """
    hashtags: Counter = Counter()
    mentions: Counter = Counter()
    linked_domains: Counter = Counter()
    channels: Dict[str, Dict[str, Any]] = {}

    for post in posts:
        text = post.get("text") or ""
        hashtags.update(match.casefold() for match in re.findall(r"#([\w-]+)", text))
        mentions.update(match.casefold() for match in re.findall(r"@([A-Za-z0-9_]{5,})", text))

        for raw_url in re.findall(r"https?://[^\s<>\"']+", text):
            cleaned_url = raw_url.rstrip(".,;:!?)]}，。；：！？）】》")
            domain = urlparse(cleaned_url).netloc.casefold()
            if domain:
                linked_domains[domain] += 1

        channel = post.get("channel") or {}
        channel_key = channel.get("username") or str(channel.get("id") or "")
        if channel_key:
            if channel_key not in channels:
                channels[channel_key] = {
                    "id": channel.get("id"),
                    "title": channel.get("title"),
                    "username": channel.get("username"),
                    "posts": 0,
                }
            channels[channel_key]["posts"] += 1

    def ranked(counter: Counter, limit: int = 30) -> List[Dict[str, Any]]:
        return [{"value": value, "count": count} for value, count in counter.most_common(limit)]

    return {
        "hashtags": ranked(hashtags),
        "mentions": ranked(mentions),
        "linked_domains": ranked(linked_domains),
        "channels": sorted(channels.values(), key=lambda item: (-item["posts"], item.get("username") or "")),
    }


# search_gifs 搜到的完整 GIF 文档，按编号存着给 send_message(gif_id=...) 用：
# 发文档要带 access_hash + file_reference，光有编号发不出去。进程内缓存，重启就空。
GIF_DOCS: Dict[int, Any] = {}



# ---------- 文件路径白名单 ----------
# 发文件、下载、换头像这几个工具会读写本机文件，只许碰「允许的目录」里的路径：
#   · HTTP 常驻模式：只认启动参数给的目录（无状态，没法反问窗口要目录）
#   · stdio 模式：优先问客户端的 MCP Roots；客户端不支持就回退到启动参数；给了空列表 = 全部拒绝
# 没配任何目录时这几个工具直接拒绝，而不是放开整块硬盘。

FILE_RULES: Dict[str, Tuple[Optional[set], int]] = {  # 用途 -> (允许后缀, 最大字节)
    "file": (None, 200 * 1024 * 1024),
    "voice": ({".ogg", ".opus"}, 100 * 1024 * 1024),
    "sticker": ({".webp"}, 10 * 1024 * 1024),
    "photo": ({".jpg", ".jpeg", ".png", ".webp"}, 50 * 1024 * 1024),
}
DOWNLOAD_SUBDIR = "downloads"
_FORBIDDEN = ("*", "?", "[", "]", "{", "}", "~", "\x00")


def _forbidden_pattern(raw: str) -> Optional[str]:
    value = raw.strip()
    if not value:
        return "Path must not be empty."
    if any(t in value for t in _FORBIDDEN):
        return "Path contains disallowed wildcard/shell patterns."
    if ".." in Path(value).parts:
        return "Path traversal is not allowed."
    return None


def _root_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"Unsupported root URI scheme: {parsed.scheme}")
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc != "localhost":
        path = f"//{parsed.netloc}{path}"
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]  # file:///C:/tmp -> C:/tmp
    return Path(path).resolve(strict=True)


def _within(candidate: Path, roots: List[Path]) -> bool:
    for root in roots:
        root = root.resolve()
        if candidate == root or (root.is_dir() and root in candidate.parents):
            return True
    return False


def _roots_unsupported(error: Exception) -> bool:
    if isinstance(error, McpError):
        err = getattr(error, "error", None)
        msg = (getattr(err, "message", None) or str(error)).lower()
        return getattr(err, "code", None) == -32601 or "method not found" in msg or "not implemented" in msg
    return isinstance(error, NotImplementedError) or (isinstance(error, AttributeError) and "list_roots" in str(error))


async def _allowed_roots(ctx: Optional[Context]) -> Tuple[List[Path], Optional[str]]:
    """返回（允许的目录, 拒绝原因）。"""
    server_roots = list(app.ALLOWED_ROOTS)
    not_configured = "File tools are disabled until allowed roots are configured (server CLI roots and/or client MCP Roots)."
    if ctx is None or app.HTTP_MODE:
        return (server_roots, None) if server_roots else ([], not_configured)
    try:
        result = await ctx.session.list_roots()
    except Exception as e:
        if _roots_unsupported(e):
            return (server_roots, None) if server_roots else ([], not_configured)
        logger.error("MCP roots request failed; disabling file tools for safety.", exc_info=True)
        return [], "File tools are disabled because MCP Roots could not be verified safely."
    roots: List[Path] = []
    for root in result.roots:
        try:
            p = _root_uri_to_path(str(root.uri))
        except Exception:
            continue  # 客户端给的坏条目直接跳过
        if p not in roots:
            roots.append(p)
    if roots:
        return roots, None
    return [], "File tools are disabled because the client provided an empty MCP Roots list (deny-all)."


def _base_dir(roots: List[Path]) -> Path:
    first = roots[0]
    return first if first.is_dir() else first.parent


async def readable_path(raw: str, ctx: Optional[Context], kind: str = "file") -> Tuple[Optional[Path], Optional[str]]:
    """要读的本机文件：必须在允许的目录里、存在、可读、后缀和大小符合 kind 的规则。返回（路径, 错误）。"""
    roots, err = await _allowed_roots(ctx)
    if err:
        return None, err
    err = _forbidden_pattern(raw)
    if err:
        return None, err
    candidate = Path(raw.strip())
    if not candidate.is_absolute():
        candidate = _base_dir(roots) / candidate
    try:
        candidate = candidate.resolve(strict=True)
    except FileNotFoundError:
        return None, f"File not found: {raw}"
    if not _within(candidate, roots):
        return None, "Path is outside allowed roots."
    if not candidate.is_file():
        return None, f"Path is not a file: {candidate}"
    if not os.access(candidate, os.R_OK):
        return None, f"File is not readable: {candidate}"
    suffixes, max_bytes = FILE_RULES[kind]
    if suffixes and candidate.suffix.lower() not in suffixes:
        return None, f"File extension is not allowed for {kind}. Allowed: {', '.join(sorted(suffixes))}."
    size = candidate.stat().st_size
    if size > max_bytes:
        return None, f"File is too large for {kind}: {size} bytes (limit: {max_bytes} bytes)."
    return candidate, None


async def writable_path(raw: Optional[str], default_name: str, ctx: Optional[Context]) -> Tuple[Optional[Path], Optional[str]]:
    """要写的本机文件：必须落在允许的目录里；不给路径就放到第一个目录的 downloads/ 下。返回（路径, 错误）。"""
    roots, err = await _allowed_roots(ctx)
    if err:
        return None, err
    if raw and raw.strip():
        err = _forbidden_pattern(raw)
        if err:
            return None, err
        candidate = Path(raw.strip())
        if not candidate.is_absolute():
            candidate = _base_dir(roots) / candidate
    else:
        candidate = _base_dir(roots) / DOWNLOAD_SUBDIR / Path(default_name).name
    candidate = candidate.resolve(strict=False)
    parent = candidate.parent.resolve(strict=False)
    if not _within(candidate, roots) or not _within(parent, roots):
        return None, "Path is outside allowed roots."
    parent.mkdir(parents=True, exist_ok=True)
    if not os.access(parent, os.W_OK):
        return None, f"Directory not writable: {parent}"
    return candidate, None


# ---------- 调研用的身份信号（sockpuppet.py / market_watch.py / trace.py 共用） ----------

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

# 太通用、串起来没有意义的域名（社交平台、官网、短链）
GENERIC_HOSTS = {
    "t.me", "telegram.org", "telegram.me", "x.com", "twitter.com", "github.com",
    "openai.com", "chatgpt.com", "anthropic.com", "claude.ai", "google.com",
    "youtube.com", "bit.ly", "linux.do", "docs.google.com", "gemini.google.com",
}

# Jack 在《卖家靠谱度核查》里手工用的负面词，原样沿用以保持结论可比
NEGATIVE_WORDS = [
    "骗子", "被骗", "曝光", "跑路", "拉黑", "不稳", "掉订阅",
    "无质保", "不到账", "退款", "维权", "失联", "假的", "垃圾",
]


def norm_url(u: str) -> str:
    """归一到「主机 + 路径」。保留 /shop/<名字> —— 卡网是多租户的，
    同一个平台上不同店铺是不同的人，按域名株连会把正规平台整个拉黑
    （实测教训：pay.ldxp.cn 是正规的链动小铺，被仿冒的是 play.ldxp.demo-fake.cn）。"""
    u = u.strip().rstrip(".,;)]}】》")
    m = re.match(r"https?://([^/\s]+)(/[^\s?#]*)?", u, re.I)
    if not m:
        return ""
    host = re.sub(r"^www\.", "", m.group(1).lower())  # 不能用 lstrip("www.")：它会把 web.x.com 剥成 eb.x.com
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


def name_kin(a: str, b: str) -> bool:
    """handle 是否像同一个人起的：去掉尾部数字和 bot/官方等后缀后相同。
    例 main_handle / main_handle77、catshop / catshop666、somebrand / somebrand_bot。"""
    def stem(x):
        return re.sub(r"(?:_?bot|官方|admin|[0-9]+)+$", "", x)
    sa, sb = stem(a), stem(b)
    return len(sa) >= 4 and sa == sb and a != b
