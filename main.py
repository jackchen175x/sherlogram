import os
import sys
import json
import time
import asyncio
import sqlite3
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from enum import Enum
from typing import List, Dict, Optional, Union, Any

# Third-party libraries
import nest_asyncio
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pythonjsonlogger.json import JsonFormatter
from telethon import TelegramClient, functions, types, utils
from telethon.sessions import StringSession
from telethon.tl.types import (
    User,
    Chat,
    Channel,
    DialogFilter,
    DialogFilterChatlist,
    DialogFilterDefault,
    TextWithEntities,
)
import re
from functools import wraps
import telethon.errors.rpcerrorlist


class ValidationError(Exception):
    """Custom exception for validation errors."""

    pass


def json_serializer(obj):
    """Helper function to convert non-serializable objects for JSON serialization."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    # Add other non-serializable types as needed
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def get_entity_type(entity: Any) -> str:
    """Return a normalized, human-readable chat/entity type."""
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
    """Return list_chats-compatible filter type: user/group/channel."""
    entity_type = get_entity_type(entity)
    if entity_type == "User":
        return "user"
    if entity_type in ("Group (Basic)", "Group", "Supergroup"):
        return "group"
    if entity_type == "Channel":
        return "channel"
    return None


load_dotenv()

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME")

# Check if a string session exists in environment, otherwise use file-based session
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")

mcp = FastMCP("telegram")

if SESSION_STRING:
    # Use the string session if available
    client = TelegramClient(StringSession(SESSION_STRING), TELEGRAM_API_ID, TELEGRAM_API_HASH)
else:
    # Use file-based session
    client = TelegramClient(TELEGRAM_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)

# Setup robust logging with both file and console output
logger = logging.getLogger("telegram_mcp")
# 默认 WARNING 而非 ERROR：重连、限速、心跳恢复都是 warning 级，
# 之前它们永远不落盘，"到底被限速过几次"是纯黑箱 —— 而这个日志是唯一故障线索。
_log_level = getattr(logging, os.getenv("LOG_LEVEL", "WARNING").upper(), logging.WARNING)
logger.setLevel(_log_level)

# Create console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(_log_level)

# Create file handler with absolute path
script_dir = os.path.dirname(os.path.abspath(__file__))
# 可用 TG_MCP_LOG 改写路径：跑测试时指向别处，否则测试噪音会淹掉真实故障线索
# （实测该日志 65% 的行来自测试里的 dummy_function 和缺 transport 的告警）
log_file_path = os.getenv("TG_MCP_LOG", os.path.join(script_dir, "mcp_errors.log"))

try:
    file_handler = logging.FileHandler(log_file_path, mode="a")  # Append mode
    file_handler.setLevel(logging.WARNING)

    # Create formatters
    # Console formatter remains in the old format
    console_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    console_handler.setFormatter(console_formatter)

    # File formatter is now JSON
    json_formatter = JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    file_handler.setFormatter(json_formatter)

    # Add handlers to logger
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.info(f"Logging initialized to {log_file_path}")
except Exception as log_error:
    print(f"WARNING: Error setting up log file: {log_error}", file=sys.stderr)
    # Fallback to console-only logging
    logger.addHandler(console_handler)
    logger.error(f"Failed to set up log file handler: {log_error}")


# 全电脑只开一份（2026-10-23）：launchd 用 --http 常驻一份，各 Claude 窗口用网址连它。
# 以前每个窗口各起一份 stdio，十来份共用一串登录凭证互相挤下线，其中一份涨到 100G。
HTTP_PORT = int(os.environ.get("TG_MCP_HTTP_PORT", "8799"))
HTTP_MODE = False


# Error code prefix mapping for better error tracing
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


def log_and_format_error(
    function_name: str,
    error: Exception,
    prefix: Optional[Union[ErrorCategory, str]] = None,
    user_message: str = None,
    **kwargs,
) -> str:
    """
    Centralized error handling function.

    Logs an error and returns a formatted, user-friendly message.

    Args:
        function_name: Name of the function where the error occurred.
        error: The exception that was raised.
        prefix: Error code prefix (e.g., ErrorCategory.CHAT, "VALIDATION-001").
            If None, it will be derived from the function_name.
        user_message: A custom user-facing message to return. If None, a generic one is created.
        **kwargs: Additional context parameters to include in the log.

    Returns:
        A user-friendly error message with an error code.
    """
    # Generate a consistent error code
    if isinstance(prefix, str) and prefix == "VALIDATION-001":
        # Special case for validation errors
        error_code = prefix
    else:
        if prefix is None:
            # Try to derive prefix from function name
            for category in ErrorCategory:
                if category.name.lower() in function_name.lower():
                    prefix = category
                    break

        prefix_str = prefix.value if isinstance(prefix, ErrorCategory) else (prefix or "GEN")
        # 曾经用 abs(hash(function_name)) % 1000 —— Python 的 str hash 每进程加盐，
        # 同一个函数每次重启都是不同数字，日志里的 GEN-ERR-062 事后根本复现不出来。
        error_code = f"{prefix_str}-ERR-{function_name}"

    # Format the additional context parameters
    context = ", ".join(f"{k}={v}" for k, v in kwargs.items())

    # Log the full technical error
    logger.error(f"Error in {function_name} ({context}) - Code: {error_code}", exc_info=True)

    # 以异常抛出，而不是 return 一段字符串。
    # 返回字符串时 MCP 协议层的 isError 是 False，agent 在批量调查里会把
    # "连接断了" 当成 "这个卖家没有负面记录" 一样的无害空结果顺手略过 ——
    # 对一个用来判断卖家可不可信的工具，这是最贵的失败模式。
    # 抛异常后 FastMCP 会置 isError=True 并保留原文。117 处调用点全在 return
    # 位置，所以改这一处即可全部翻转，调用点一行不动。
    if user_message:
        msg = user_message
    else:
        classified = classify_error(error)
        msg = (
            f"{classified} (code: {error_code})"
            if classified
            else f"An error occurred (code: {error_code}). Check mcp_errors.log for details."
        )
    raise RuntimeError(msg) from error


def validate_id(*param_names_to_validate):
    """
    Decorator to validate chat_id and user_id parameters, including lists of IDs.
    It checks for valid integer ranges, string representations of integers,
    and username formats.
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for param_name in param_names_to_validate:
                if param_name not in kwargs or kwargs[param_name] is None:
                    continue

                param_value = kwargs[param_name]

                def validate_single_id(value, p_name):
                    # Handle integer IDs
                    if isinstance(value, int):
                        if not (-(2**63) <= value <= 2**63 - 1):
                            return (
                                None,
                                f"Invalid {p_name}: {value}. ID is out of the valid integer range.",
                            )
                        return value, None

                    # Handle string IDs
                    if isinstance(value, str):
                        try:
                            int_value = int(value)
                            if not (-(2**63) <= int_value <= 2**63 - 1):
                                return (
                                    None,
                                    f"Invalid {p_name}: {value}. ID is out of the valid integer range.",
                                )
                            return int_value, None
                        except ValueError:
                            if re.match(r"^@?[a-zA-Z0-9_]{5,}$", value):
                                return value, None
                            else:
                                return (
                                    None,
                                    f"Invalid {p_name}: '{value}'. Must be a valid integer ID, or a username string.",
                                )

                    # Handle other invalid types
                    return (
                        None,
                        f"Invalid {p_name}: {value}. Type must be an integer or a string.",
                    )

                if isinstance(param_value, list):
                    validated_list = []
                    for item in param_value:
                        validated_item, error_msg = validate_single_id(item, param_name)
                        if error_msg:
                            return log_and_format_error(
                                func.__name__,
                                ValidationError(error_msg),
                                prefix="VALIDATION-001",
                                user_message=error_msg,
                                **{param_name: param_value},
                            )
                        validated_list.append(validated_item)
                    kwargs[param_name] = validated_list
                else:
                    validated_value, error_msg = validate_single_id(param_value, param_name)
                    if error_msg:
                        return log_and_format_error(
                            func.__name__,
                            ValidationError(error_msg),
                            prefix="VALIDATION-001",
                            user_message=error_msg,
                            **{param_name: param_value},
                        )
                    kwargs[param_name] = validated_value

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def format_entity(entity) -> Dict[str, Any]:
    """Helper function to format entity information consistently."""
    result = {"id": entity.id}

    if hasattr(entity, "title"):
        result["name"] = entity.title
        result["type"] = "group" if isinstance(entity, Chat) else "channel"
    elif hasattr(entity, "first_name"):
        name_parts = []
        if entity.first_name:
            name_parts.append(entity.first_name)
        if hasattr(entity, "last_name") and entity.last_name:
            name_parts.append(entity.last_name)
        result["name"] = " ".join(name_parts)
        result["type"] = "user"
        if hasattr(entity, "phone") and entity.phone:
            result["phone"] = entity.phone

    # username 对 User/Chat/Channel 统一输出。以前它写在 user 分支里，导致
    # search_public_chats 找到的频道只有 id/name/type —— 拿不到 @username
    # 就无法 resolve、无法 get_history，深挖在第一跳就断了。
    username = getattr(entity, "username", None)
    if username:
        result["username"] = username

    # 官方信任标记：高精度、低召回（实测大量骗子号 scam 仍为 False），
    # 只在为 True 时输出，避免模型把 False 读成"已核验安全"。
    for flag in ("scam", "fake", "restricted", "verified"):
        if getattr(entity, flag, False):
            result[flag] = True

    return result


async def ensure_connected(max_retries: int = 3, delay: float = 2.0):
    """确保 Telegram client 已连接，断连时自动重连"""
    if client.is_connected():
        return
    for attempt in range(max_retries):
        try:
            logger.warning(f"Client disconnected, reconnecting (attempt {attempt+1}/{max_retries})...")
            await client.connect()
            if client.is_connected():
                logger.info("Reconnected successfully")
                return
        except Exception as e:
            logger.error(f"Reconnect attempt {attempt+1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(delay * (attempt + 1))
    raise ConnectionError("Failed to reconnect to Telegram after all retries")


def classify_error(e: Exception) -> str:
    """将 Telegram 异常分类为用户友好的错误消息"""
    error_str = str(e)
    error_type = type(e).__name__

    if isinstance(e, ConnectionError) or "disconnected" in error_str.lower():
        return "Connection lost. The client will auto-reconnect on next request."
    if "FloodWaitError" in error_type or "flood" in error_str.lower():
        # 提取等待秒数
        import re
        m = re.search(r'(\d+)', error_str)
        wait = m.group(1) if m else "unknown"
        return f"Rate limited by Telegram. Please wait {wait} seconds before retrying."
    if "AuthKeyError" in error_type or "Unauthorized" in error_type or "SESSION_REVOKED" in error_str:
        return "Session expired or revoked. Please regenerate your session string."
    if "ChatAdminRequiredError" in error_type:
        return "Admin privileges required for this operation."
    if "UserNotParticipantError" in error_type:
        return "You are not a member of this chat."
    if "PeerIdInvalidError" in error_type or "PeerIdInvalid" in error_str:
        return "Invalid chat/user ID. Please verify the ID is correct."
    if isinstance(e, asyncio.TimeoutError) or isinstance(e, RuntimeError) and "timed out" in error_str:
        return "Request timed out. Telegram may be slow — please retry."

    return ""  # 无特殊分类，走默认处理


# Entity 缓存：避免每次 resolve 失败都拉全量 dialogs
_entity_cache: Dict[Union[int, str], Any] = {}


async def resolve_entity(identifier: Union[int, str]) -> Any:
    """Resolve entity with local cache + automatic cache warming on miss."""
    await ensure_connected()
    # 先查本地缓存
    if identifier in _entity_cache:
        return _entity_cache[identifier]
    try:
        entity = await client.get_entity(identifier)
        _entity_cache[identifier] = entity
        return entity
    except ValueError:
        # 缓存冷启动，拉 dialogs 预热
        await client.get_dialogs()
        entity = await client.get_entity(identifier)
        _entity_cache[identifier] = entity
        return entity


async def resolve_input_entity(identifier: Union[int, str]) -> Any:
    """Like resolve_entity() but returns an InputPeer."""
    await ensure_connected()
    try:
        return await client.get_input_entity(identifier)
    except ValueError:
        await client.get_dialogs()
        return await client.get_input_entity(identifier)


def format_message(message) -> Dict[str, Any]:
    """Helper function to format message information consistently."""
    result = {
        "id": message.id,
        "date": message.date.isoformat(),
        "text": message.message or "",
    }

    if message.from_id:
        result["from_id"] = utils.get_peer_id(message.from_id)

    if message.media:
        result["has_media"] = True
        result["media_type"] = type(message.media).__name__

    return result


def get_sender_name(message) -> str:
    """Helper function to get sender name from a message, including @username when available."""
    if not message.sender:
        return "Unknown"

    # Check for group/channel title first
    if hasattr(message.sender, "title") and message.sender.title:
        username = getattr(message.sender, "username", None)
        if username:
            return f"{message.sender.title} (@{username})"
        return message.sender.title
    elif hasattr(message.sender, "first_name"):
        # User sender
        first_name = getattr(message.sender, "first_name", "") or ""
        last_name = getattr(message.sender, "last_name", "") or ""
        full_name = f"{first_name} {last_name}".strip()
        username = getattr(message.sender, "username", None)
        if username:
            return f"{full_name or 'Unknown'} (@{username})"
        return full_name if full_name else "Unknown"
    else:
        return "Unknown"


def get_provenance_info(message) -> str:
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
        if getattr(fwd, "from_name", None):
            parts.append(f"fwd_from_name:{fwd.from_name}")
        if getattr(fwd, "channel_post", None):
            parts.append(f"fwd_channel_post:{fwd.channel_post}")
        if getattr(fwd, "post_author", None):
            parts.append(f"fwd_author:{fwd.post_author}")
        # 原帖发布时间，以及它比这次转发早了多少天
        fwd_date = getattr(fwd, "date", None)
        if fwd_date:
            parts.append(f"fwd_date:{fwd_date:%Y-%m-%d}")
            msg_date = getattr(message, "date", None)
            if msg_date:
                age = (msg_date - fwd_date).days
                if age >= 1:
                    parts.append(f"fwd_age_days:{age}")

    edited = getattr(message, "edit_date", None)
    if edited:
        parts.append(f"edited:{edited:%Y-%m-%d}")

    if getattr(message, "via_bot_id", None):
        parts.append(f"via_bot:{message.via_bot_id}")

    return f" | provenance=[{', '.join(parts)}]" if parts else ""


def get_engagement_info(message) -> str:
    """Helper function to get engagement metrics (views, forwards, reactions) from a message."""
    engagement_parts = []
    views = getattr(message, "views", None)
    if views is not None:
        engagement_parts.append(f"views:{views}")
    forwards = getattr(message, "forwards", None)
    if forwards is not None:
        engagement_parts.append(f"forwards:{forwards}")
    reactions = getattr(message, "reactions", None)
    if reactions is not None:
        results = getattr(reactions, "results", None)
        total_reactions = sum(getattr(r, "count", 0) or 0 for r in results) if results else 0
        engagement_parts.append(f"reactions:{total_reactions}")
    return f" | {', '.join(engagement_parts)}" if engagement_parts else ""


@mcp.tool(
    annotations=ToolAnnotations(title="Send Message", openWorldHint=True, destructiveHint=True)
)
@validate_id("chat_id")
async def send_message(
    chat_id: Union[int, str], message: str, parse_mode: Optional[str] = None
) -> str:
    """
    Send a message to a specific chat.
    Args:
        chat_id: The ID or username of the chat.
        message: The message content to send.
        parse_mode: Optional formatting mode. Use 'html' for HTML tags (<b>, <i>, <code>, <pre>,
            <a href="...">), 'md' or 'markdown' for Markdown (**bold**, __italic__, `code`,
            ```pre```), or omit for plain text (no formatting).
    """
    try:
        entity = await resolve_entity(chat_id)
        await client.send_message(entity, message, parse_mode=parse_mode)
        return "Message sent successfully."
    except Exception as e:
        return log_and_format_error("send_message", e, chat_id=chat_id)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Subscribe Public Channel",
        openWorldHint=True,
        destructiveHint=True,
        idempotentHint=True,
    )
)
@validate_id("channel")
async def subscribe_public_channel(channel: Union[int, str]) -> str:
    """
    Subscribe (join) to a public channel or supergroup by username or ID.
    """
    try:
        entity = await resolve_entity(channel)
        await client(functions.channels.JoinChannelRequest(channel=entity))
        title = getattr(entity, "title", getattr(entity, "username", "Unknown channel"))
        return f"Subscribed to {title}."
    except telethon.errors.rpcerrorlist.UserAlreadyParticipantError:
        title = getattr(entity, "title", getattr(entity, "username", "this channel"))
        return f"Already subscribed to {title}."
    except telethon.errors.rpcerrorlist.ChannelPrivateError:
        return "Cannot subscribe: this channel is private or requires an invite link."
    except Exception as e:
        return log_and_format_error("subscribe_public_channel", e, channel=channel)


@mcp.tool(
    annotations=ToolAnnotations(title="List Messages", openWorldHint=True, readOnlyHint=True)
)
@validate_id("chat_id")
async def list_messages(
    chat_id: Union[int, str],
    limit: int = 20,
    search_query: str = None,
    from_date: str = None,
    to_date: str = None,
    from_user: Union[int, str] = None,
    full_text: bool = False,
) -> str:
    """
    Read messages from one chat: the latest N, a keyword search, a date range,
    or one sender's posts.

    Args:
        chat_id: The ID or username of the chat to get messages from.
        limit: Maximum number of messages to retrieve.
        search_query: Filter messages containing this text.
        full_text: Return message bodies in full. Off by default — long ads are
            clipped to 400 chars with the cut size shown, so a 100-message page
            stays affordable. Turn on when you need a specific ad verbatim.
        from_user: Only messages sent by this user/channel (ID or @username).
            Use this to aggregate one seller's posts inside a busy group —
            searching for their @handle does NOT work, because their own ads
            rarely contain it.
        from_date: Filter messages starting from this date (format: YYYY-MM-DD).
        to_date: Filter messages until this date (format: YYYY-MM-DD).
    """
    try:
        entity = await resolve_entity(chat_id)

        try:
            from_date_obj = (
                datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if from_date
                else None
            )
            # to_date 含当天：取到当天最后一微秒
            to_date_obj = (
                datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                + timedelta(days=1, microseconds=-1)
                if to_date
                else None
            )
        except ValueError:
            return "Invalid date format. Use YYYY-MM-DD."

        # Prepare filter parameters
        params = {}
        if from_user:
            # 走 resolve_entity 而不是直接透传：它带进程内缓存，批量按卖家聚合时
            # 不会对同一个 handle 反复发 ResolveUsernameRequest（该请求是已知的封号源）。
            params["from_user"] = await resolve_entity(from_user)
        if search_query:
            # IMPORTANT: Do not combine offset_date with search.
            # Use server-side search alone, then enforce date bounds client-side.
            params["search"] = search_query
            messages = []
            async for msg in client.iter_messages(entity, **params):  # newest -> oldest
                if to_date_obj and msg.date > to_date_obj:
                    continue
                if from_date_obj and msg.date < from_date_obj:
                    break
                messages.append(msg)
                if len(messages) >= limit:
                    break

        else:
            # Use server-side iteration when only date bounds are present
            # (no search) to avoid over-fetching.
            if from_date_obj or to_date_obj:
                messages = []
                if from_date_obj:
                    # Walk forward from start date (oldest -> newest)
                    async for msg in client.iter_messages(
                        entity, offset_date=from_date_obj, reverse=True, **params
                    ):
                        if to_date_obj and msg.date > to_date_obj:
                            break
                        if msg.date < from_date_obj:
                            continue
                        messages.append(msg)
                        if len(messages) >= limit:
                            break
                else:
                    # Only upper bound: walk backward from end bound
                    async for msg in client.iter_messages(
                        # offset_date is exclusive; +1µs makes to_date inclusive
                        entity,
                        offset_date=to_date_obj + timedelta(microseconds=1),
                        **params,
                    ):
                        messages.append(msg)
                        if len(messages) >= limit:
                            break
            else:
                messages = await client.get_messages(entity, limit=limit, **params)

        if not messages:
            return "No messages found matching the criteria."

        lines = []
        for msg in messages:
            sender_name = get_sender_name(msg)
            message_text = _clip(msg.message, full_text) or "[Media/No text]"
            reply_info = ""
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                reply_info = f" | reply to {msg.reply_to.reply_to_msg_id}"

            engagement_info = get_engagement_info(msg)
            forward_info = get_provenance_info(msg)

            lines.append(
                f"ID: {msg.id} | {sender_name} | Date: {msg.date}{reply_info}{forward_info}{engagement_info} | Message: {message_text}"
            )

        return "\n".join(lines) + _clues_tail(messages)
    except Exception as e:
        return log_and_format_error("list_messages", e, chat_id=chat_id)


@mcp.tool(annotations=ToolAnnotations(title="List Chats", openWorldHint=True, readOnlyHint=True))
async def list_chats(
    chat_type: str = None, limit: int = 20, unread_only: bool = False, unmuted_only: bool = False
) -> str:
    """
    List available chats with metadata.

    Args:
        chat_type: Filter by chat type ('user', 'group', 'channel', or None for all)
        limit: Maximum number of chats to retrieve from Telegram API (applied before filtering, so fewer results may be returned when filters are active).
        unread_only: If True, only return chats with unread messages.
        unmuted_only: If True, only return unmuted chats.
    """
    try:
        dialogs = await client.get_dialogs(limit=limit)

        results = []
        for dialog in dialogs:
            entity = dialog.entity

            # Filter by type if requested
            current_type = get_entity_filter_type(entity)

            if chat_type and current_type != chat_type.lower():
                continue

            # Format chat info
            chat_info = f"Chat ID: {entity.id}"

            if hasattr(entity, "title"):
                chat_info += f", Title: {entity.title}"
            elif hasattr(entity, "first_name"):
                name = f"{entity.first_name}"
                if hasattr(entity, "last_name") and entity.last_name:
                    name += f" {entity.last_name}"
                chat_info += f", Name: {name}"

            chat_info += f", Type: {get_entity_type(entity)}"

            if hasattr(entity, "username") and entity.username:
                chat_info += f", Username: @{entity.username}"

            # Add unread count if available
            unread_count = getattr(dialog, "unread_count", 0) or 0
            # Also check unread_mark (manual "mark as unread" flag)
            inner_dialog = getattr(dialog, "dialog", None)
            unread_mark = (
                bool(getattr(inner_dialog, "unread_mark", False)) if inner_dialog else False
            )

            # Extract mute status from notify_settings
            notify_settings = getattr(inner_dialog, "notify_settings", None)
            mute_until = getattr(notify_settings, "mute_until", None)
            if mute_until is None:
                is_muted = False
            elif isinstance(mute_until, datetime):
                is_muted = mute_until.timestamp() > time.time()
            else:
                is_muted = mute_until > time.time()

            # Filter by mute status if requested
            if unmuted_only and is_muted:
                continue

            # Filter by unread status if requested
            if unread_only and unread_count == 0 and not unread_mark:
                continue

            if unread_count > 0:
                chat_info += f", Unread: {unread_count}"
            elif unread_mark:
                chat_info += ", Unread: marked"
            else:
                chat_info += ", No unread messages"

            chat_info += f", Muted: {'yes' if is_muted else 'no'}"

            # Add unread mentions count if available
            unread_mentions = getattr(dialog, "unread_mentions_count", 0) or 0
            if unread_mentions > 0:
                chat_info += f", Unread mentions: {unread_mentions}"

            results.append(chat_info)

        if not results:
            return f"No chats found matching the criteria."

        return "\n".join(results)
    except Exception as e:
        return log_and_format_error(
            "list_chats",
            e,
            chat_type=chat_type,
            limit=limit,
            unread_only=unread_only,
            unmuted_only=unmuted_only,
        )


@mcp.tool(annotations=ToolAnnotations(title="Get Chat", openWorldHint=True, readOnlyHint=True))
@validate_id("chat_id")
async def get_chat(chat_id: Union[int, str]) -> str:
    """
    Get details of a group, channel, user or bot: type, members, flags (scam/fake),
    opacity signals, user bio and common-chat count, unread count, last message.

    Args:
        chat_id: The ID or username of the chat or user.
    """
    try:
        entity = await resolve_entity(chat_id)

        result = []
        result.append(f"ID: {entity.id}")

        is_user = isinstance(entity, User)

        if hasattr(entity, "title"):
            result.append(f"Title: {entity.title}")
            result.append(f"Type: {get_entity_type(entity)}")
            if hasattr(entity, "username") and entity.username:
                result.append(f"Username: @{entity.username}")

            # Fetch participants count reliably
            try:
                participants_count = (await client.get_participants(entity, limit=0)).total
                result.append(f"Participants: {participants_count}")
            except Exception as pe:
                result.append(f"Participants: Error fetching ({pe})")

            flags = [f for f in ("scam", "fake", "restricted", "verified")
                     if getattr(entity, f, False)]
            if flags:
                result.append(f"Flags: {', '.join(flags)}")

            # 结构不透明度：以下每一项都是卖家主动关闭一条核查路径。
            # 「卖家让你查不了」这个行为本身，比任何官方标记都更能说明问题 ——
            # 而在此之前，一个三重不透明的卖货群和一个全开放群在输出里毫无差别。
            opacity = []
            if getattr(entity, "noforwards", False):
                opacity.append("禁止转发/截图外传（你无法把证据转给曝光频道）")
            try:
                full_chat = (await client(functions.channels.GetFullChannelRequest(entity))).full_chat
                if getattr(full_chat, "hidden_prehistory", False):
                    opacity.append("新成员看不到入群前历史（旧差评对新买家不可见）")
                if getattr(full_chat, "participants_hidden", False):
                    opacity.append("成员列表已隐藏")
                elif not getattr(full_chat, "can_view_participants", True):
                    opacity.append("不可查看成员（无法核对成员真实性）")
                if getattr(entity, "broadcast", False) and not getattr(
                    full_chat, "linked_chat_id", None
                ):
                    opacity.append("没有评论区 —— 不是「零差评」，是结构上不可能有公开差评")
            except Exception as fe:
                logger.warning(f"GetFullChannel failed for {chat_id}: {fe}")
            if opacity:
                result.append(f"Opacity ({len(opacity)}): " + "; ".join(opacity))

        elif is_user:
            name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
            result.append(f"Name: {name}")
            result.append(f"Type: {'Bot' if entity.bot else 'User'}")
            if entity.username:
                result.append(f"Username: @{entity.username}")
            if entity.phone:
                result.append(f"Phone: {entity.phone}")
            # 实体缓存没有过期时间，scam/fake 可能是旧值；GetFullUser 顺带回来的 User 是新的
            try:
                full = await client(functions.users.GetFullUserRequest(id=entity))
                fresh = next((u for u in full.users if u.id == entity.id), entity)
                fu = full.full_user
                if fu.about:
                    result.append(f"Bio: {fu.about}")
                result.append(f"Common chats: {fu.common_chats_count}")
                if fu.bot_info and fu.bot_info.description:
                    result.append(f"Bot description: {fu.bot_info.description}")
            except Exception as fe:
                fresh = entity
                logger.warning(f"GetFullUser failed for {chat_id}: {fe}")
            flags = [f for f in ("scam", "fake", "verified", "premium", "restricted")
                     if getattr(fresh, f, False)]
            if flags:
                result.append(f"Flags: {', '.join(flags)}")

        # Unread counts only exist for chats that are actually in the account's
        # dialog list.  ``offset_peer`` is a pagination cursor, not a filter;
        # using it with ``limit=1`` can return the next unrelated dialog.
        try:
            # 单点查询，而不是拉回全部会话再过滤 —— get_chat 是调研里最高频的动词，
            # 每次都全量扫一遍会话列表是白花的 RPC。
            peer = await resolve_input_entity(chat_id)
            res = await client(
                functions.messages.GetPeerDialogsRequest(
                    peers=[types.InputDialogPeer(peer=peer)]
                )
            )
            dialog = res.dialogs[0] if getattr(res, "dialogs", None) else None
            if dialog is not None:
                result.append(f"Unread Messages: {dialog.unread_count}")
        except Exception as diag_ex:
            logger.warning(f"Could not get dialog info for {chat_id}: {diag_ex}")

        # Fetch the latest message from the resolved target itself.  This also
        # works for accessible public channels that the account has not joined.
        try:
            messages = await client.get_messages(entity, limit=1)
            if messages:
                last_msg = messages[0]
                sender_name = "Unknown"
                if last_msg.sender:
                    sender_name = getattr(last_msg.sender, "first_name", "") or getattr(
                        last_msg.sender, "title", "Unknown"
                    )
                    if hasattr(last_msg.sender, "last_name") and last_msg.sender.last_name:
                        sender_name += f" {last_msg.sender.last_name}"
                sender_name = sender_name.strip() or "Unknown"
                result.append(f"Last Message: From {sender_name} at {last_msg.date}")
                result.append(f"Message: {last_msg.message or '[Media/No text]'}")
        except Exception as message_ex:
            logger.warning(f"Could not get latest message for {chat_id}: {message_ex}")

        return "\n".join(result)
    except Exception as e:
        return log_and_format_error("get_chat", e, chat_id=chat_id)


@mcp.tool(annotations=ToolAnnotations(title="Get Me", openWorldHint=True, readOnlyHint=True))
async def get_me() -> str:
    """
    Get your own user information.
    """
    try:
        me = await client.get_me()
        return json.dumps(format_entity(me), ensure_ascii=False)
    except Exception as e:
        return log_and_format_error("get_me", e)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Leave Chat", openWorldHint=True, destructiveHint=True, idempotentHint=True
    )
)
@validate_id("chat_id")
async def leave_chat(chat_id: Union[int, str]) -> str:
    """
    Leave a group or channel by chat ID.

    Args:
        chat_id: The chat ID or username to leave.
    """
    try:
        entity = await resolve_entity(chat_id)

        # Check the entity type carefully
        if isinstance(entity, Channel):
            # Handle both channels and supergroups (which are also channels in Telegram)
            try:
                await client(functions.channels.LeaveChannelRequest(channel=entity))
                chat_name = getattr(entity, "title", str(chat_id))
                return f"Left channel/supergroup {chat_name} (ID: {chat_id})."
            except Exception as chan_err:
                return log_and_format_error("leave_chat", chan_err, chat_id=chat_id)

        elif isinstance(entity, Chat):
            # Traditional basic groups (not supergroups)
            try:
                # First try with InputPeerUser
                me = await client.get_me(input_peer=True)
                await client(
                    functions.messages.DeleteChatUserRequest(
                        chat_id=entity.id,
                        user_id=me,  # Use the entity ID directly
                    )
                )
                chat_name = getattr(entity, "title", str(chat_id))
                return f"Left basic group {chat_name} (ID: {chat_id})."
            except Exception as chat_err:
                # If the above fails, try the second approach
                logger.warning(
                    f"First leave attempt failed: {chat_err}, trying alternative method"
                )

                try:
                    # Alternative approach - sometimes this works better
                    me_full = await client.get_me()
                    await client(
                        functions.messages.DeleteChatUserRequest(
                            chat_id=entity.id, user_id=me_full.id
                        )
                    )
                    chat_name = getattr(entity, "title", str(chat_id))
                    return f"Left basic group {chat_name} (ID: {chat_id})."
                except Exception as alt_err:
                    return log_and_format_error("leave_chat", alt_err, chat_id=chat_id)
        else:
            # Cannot leave a user chat this way
            entity_type = type(entity).__name__
            return log_and_format_error(
                "leave_chat",
                Exception(
                    f"Cannot leave chat ID {chat_id} of type {entity_type}. This function is for groups and channels only."
                ),
                chat_id=chat_id,
            )

    except Exception as e:
        logger.exception(f"leave_chat failed (chat_id={chat_id})")

        # Provide helpful hint for common errors
        error_str = str(e).lower()
        if "invalid" in error_str and "chat" in error_str:
            return log_and_format_error(
                "leave_chat",
                Exception(
                    f"Error leaving chat: This appears to be a channel/supergroup. Please check the chat ID and try again."
                ),
                chat_id=chat_id,
            )

        return log_and_format_error("leave_chat", e, chat_id=chat_id)


@mcp.tool(
    annotations=ToolAnnotations(title="Get Participants", openWorldHint=True, readOnlyHint=True)
)
@validate_id("chat_id")
async def get_participants(
    chat_id: Union[int, str],
    limit: int = 100,
    search: str = None,
    role: str = None,
) -> str:
    """
    List participants in a group or channel.

    Args:
        chat_id: The group or channel ID or username.
        limit: Max participants to return (default 100). Large groups can hold
            tens of thousands — fetching all of them will blow up the context.
        search: Only participants whose name/username matches this text.
        role: One of "admins", "bots", "banned", "recent". Omit for all.
            "admins" answers a question plain membership cannot: is this seller
            the one running the group, or just someone spamming in it?
    """
    try:
        entity = await resolve_entity(chat_id)
        filters = {
            "admins": types.ChannelParticipantsAdmins(),
            "bots": types.ChannelParticipantsBots(),
            "banned": types.ChannelParticipantsKicked(q=""),
            "recent": types.ChannelParticipantsRecent(),
        }
        if role and role not in filters:
            return f"Unknown role {role!r}. Use one of: {', '.join(filters)}."

        kwargs = {"limit": limit}
        if search:
            kwargs["search"] = search
        if role:
            kwargs["filter"] = filters[role]

        participants = await client.get_participants(entity, **kwargs)

        def role_of(p):
            part = getattr(p, "participant", None)
            if isinstance(part, types.ChannelParticipantCreator):
                return "creator"
            if isinstance(part, types.ChannelParticipantAdmin):
                return "admin"
            if isinstance(part, types.ChannelParticipantBanned):
                return "banned"
            return "member"

        out = []
        for p in participants:
            row = {
                "id": p.id,
                "name": " ".join(
                    x for x in (getattr(p, "first_name", ""), getattr(p, "last_name", "")) if x
                ).strip(),
                # username 是把群成员和消息里的 @卖家 对上号的唯一钥匙，旧实现整个丢了
                "username": getattr(p, "username", None),
                "role": role_of(p),
            }
            for flag in ("bot", "premium", "scam", "fake"):
                if getattr(p, flag, False):
                    row[flag] = True
            joined = getattr(getattr(p, "participant", None), "date", None)
            if joined:
                row["joined"] = joined.strftime("%Y-%m-%d")
            out.append(row)

        return json.dumps(
            {"count": len(out), "limit": limit, "participants": out},
            ensure_ascii=False,
        )
    except Exception as e:
        return log_and_format_error("get_participants", e, chat_id=chat_id)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Join Chat By Link", openWorldHint=True, destructiveHint=True, idempotentHint=True
    )
)
async def join_chat_by_link(link: str) -> str:
    """
    Join a chat by invite link.
    """
    try:
        # Extract the hash from the invite link
        if "/" in link:
            hash_part = link.split("/")[-1]
            if hash_part.startswith("+"):
                hash_part = hash_part[1:]  # Remove the '+' if present
        else:
            hash_part = link

        # Try checking the invite before joining
        try:
            # Try to check invite info first (will often fail if not a member)
            invite_info = await client(functions.messages.CheckChatInviteRequest(hash=hash_part))
            if hasattr(invite_info, "chat") and invite_info.chat:
                # If we got chat info, we're already a member
                chat_title = getattr(invite_info.chat, "title", "Unknown Chat")
                return f"You are already a member of this chat: {chat_title}"
        except Exception:
            # This often fails if not a member - just continue
            pass

        # Join the chat using the hash
        result = await client(functions.messages.ImportChatInviteRequest(hash=hash_part))
        if result and hasattr(result, "chats") and result.chats:
            chat_title = getattr(result.chats[0], "title", "Unknown Chat")
            return f"Successfully joined chat: {chat_title}"
        return f"Joined chat via invite hash."
    except Exception as e:
        err_str = str(e).lower()
        if "expired" in err_str:
            return "The invite hash has expired and is no longer valid."
        elif "invalid" in err_str:
            return "The invite hash is invalid or malformed."
        elif "already" in err_str and "participant" in err_str:
            return "You are already a member of this chat."
        logger.exception(f"join_chat_by_link failed (link={link})")
        return f"Error joining chat: {e}"


@mcp.tool(
    annotations=ToolAnnotations(title="Search Public Chats", openWorldHint=True, readOnlyHint=True)
)
async def search_public_chats(query: str, limit: int = 20) -> str:
    """
    Search for public chats, channels, or bots by username or title.
    """
    try:
        result = await client(functions.contacts.SearchRequest(q=query, limit=limit))
        entities = [format_entity(e) for e in result.chats + result.users]
        return json.dumps(entities, ensure_ascii=False)
    except Exception as e:
        return log_and_format_error("search_public_chats", e, query=query, limit=limit)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Search Global Messages",
        openWorldHint=True,
        readOnlyHint=True,
    )
)
async def search_global(query: str, page: int = 1, page_size: int = 20) -> str:
    """
    Search for messages across chats visible to the current account.

    This uses Telegram's legacy global message search. To search posts from
    public channels the account has not joined, use search_public_posts.
    """
    try:
        offset = (page - 1) * page_size
        messages = await client.get_messages(
            None, limit=page_size, search=query, add_offset=offset
        )

        if not messages:
            return "No messages found for this page."

        lines = []
        for msg in messages:
            chat = msg.chat
            chat_name = (
                getattr(chat, "title", None) or getattr(chat, "first_name", "") or str(msg.chat_id)
            )
            chat_username = getattr(chat, "username", None)
            chat_label = f"{chat_name} (@{chat_username})" if chat_username else chat_name
            sender_name = get_sender_name(msg)
            forward_info = get_provenance_info(msg)
            lines.append(
                f"Chat: {chat_label} | ID: {msg.id} | {sender_name} | "
                f"Date: {msg.date}{forward_info} | Message: {_clip(msg.message)}"
            )

        return "\n".join(lines) + _clues_tail(messages)
    except Exception as e:
        return log_and_format_error(
            "search_global", e, query=query, page=page, page_size=page_size
        )


def _format_public_post_search_quota(search_flood: Any) -> Dict[str, Any]:
    """Convert Telegram's SearchPostsFlood result into JSON-safe quota metadata."""
    wait_till = getattr(search_flood, "wait_till", None)
    wait_till_iso = None
    if wait_till:
        wait_till_iso = datetime.fromtimestamp(wait_till).astimezone().isoformat()

    return {
        "total_daily": getattr(search_flood, "total_daily", 0),
        "remains": getattr(search_flood, "remains", 0),
        "query_is_free": bool(getattr(search_flood, "query_is_free", False)),
        "wait_till": wait_till,
        "wait_till_iso": wait_till_iso,
        "stars_required": getattr(search_flood, "stars_amount", 0),
    }


MSG_CLIP = 400


def _clip(text: str, full_text: bool = False) -> str:
    """长消息截断，但把截断这件事明说出来。

    ⚠️ 实测下来它的收益比预期小得多，别高估：
    limit=100 时全文 5681 token、截断后 5513 token，**只省 3%**。
    真正的成本大头是「消息条数 × 每行元数据」，不是长正文 —— 正文中位只有 61 字符
    （300 条样本：90 分位 174，最长 2310）。**想省上下文，调 limit 比截断有效得多。**

    那为什么还留着：偶发的 2000+ 字符刷屏广告确实存在，一页里撞上几条就很难看。
    这是兜底，不是优化手段。

    但被截掉的恰恰是信息最密的那些 —— 完整报价单、卡网链接、联系方式。所以：
      · 必须留 full_text 逃生口
      · 必须显式标出截了多少，否则就是静默截断，模型会把残缺当完整
      · 域名/@提及 的抽取在截断之前做（见 _clues_tail），结构化信号不受影响
    """
    if full_text or not text or len(text) <= MSG_CLIP:
        return text
    return f"{text[:MSG_CLIP]}…[已截断，还有 {len(text) - MSG_CLIP} 字符，full_text=True 取全文]"


def _clues_tail(messages) -> str:
    """把这批消息里高频出现的 @账号 / 域名 / 话题附在结果末尾。

    这是 corpus-first 的落地形态：调查的下一跳不该靠人再读一遍消息去捞，
    应该由上一次调用直接指出来。此前全项目只有 search_public_posts 做了这件事，
    是 98 个工具里唯一在解决"深挖"而不是"调用"的地方。

    只输出出现 ≥2 次的，否则等于给每次调用平白加一段上下文。
    """
    clues = _extract_public_post_clues(
        [{"text": getattr(msg, "message", "") or ""} for msg in messages]
    )
    parts = []
    for label, key in (("提及", "mentions"), ("域名", "linked_domains"), ("话题", "hashtags")):
        top = [x for x in clues[key] if x["count"] >= 2][:8]
        if top:
            parts.append(f"{label}: " + ", ".join(f"{x['value']}×{x['count']}" for x in top))
    if not parts:
        return ""
    return "\n\n--- 线索（本批消息里的高频项，可作为下一跳）---\n" + "\n".join(parts)


def _extract_public_post_clues(posts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract deterministic pivots for the next step of a public-post investigation."""
    hashtags: Counter[str] = Counter()
    mentions: Counter[str] = Counter()
    linked_domains: Counter[str] = Counter()
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

    def ranked(counter: Counter[str], limit: int = 30) -> List[Dict[str, Any]]:
        return [
            {"value": value, "count": count}
            for value, count in counter.most_common(limit)
        ]

    return {
        "hashtags": ranked(hashtags),
        "mentions": ranked(mentions),
        "linked_domains": ranked(linked_domains),
        "channels": sorted(
            channels.values(),
            key=lambda item: (-item["posts"], item.get("username") or ""),
        ),
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Search Public Posts (No Stars)",
        openWorldHint=True,
        readOnlyHint=True,
    )
)
async def search_public_posts(
    query: Optional[str] = None,
    hashtag: Optional[str] = None,
    limit: int = 20,
    offset_rate: int = 0,
    offset_peer: Optional[Union[int, str]] = None,
    offset_id: int = 0,
) -> str:
    """
    Search posts from all public Telegram channels, including unjoined channels.

    Exactly one of query or hashtag is required. Full-text queries use Telegram's
    limited daily search quota; hashtag searches do not use a full-text slot.
    This tool checks the quota before a first-page full-text search and never
    authorizes Telegram Star payments. Pagination calls are free according to
    Telegram and use the next_offset values returned by the previous call.
    """
    normalized_query = query.strip() if query else None
    normalized_hashtag = hashtag.strip().lstrip("#") if hashtag else None

    if bool(normalized_query) == bool(normalized_hashtag):
        return json.dumps(
            {
                "status": "invalid_request",
                "error": "Provide exactly one of query or hashtag.",
            },
            ensure_ascii=False,
        )
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        return json.dumps(
            {"status": "invalid_request", "error": "limit must be between 1 and 100."},
            ensure_ascii=False,
        )
    if offset_rate < 0 or offset_id < 0:
        return json.dumps(
            {
                "status": "invalid_request",
                "error": "offset_rate and offset_id must be non-negative.",
            },
            ensure_ascii=False,
        )
    if (offset_rate or offset_id) and offset_peer is None:
        return json.dumps(
            {
                "status": "invalid_request",
                "error": "offset_peer is required when continuing a paginated search.",
            },
            ensure_ascii=False,
        )

    try:
        quota = None
        is_first_page = offset_rate == 0 and offset_id == 0 and offset_peer is None
        if normalized_query and is_first_page:
            quota_result = await client(
                functions.channels.CheckSearchPostsFloodRequest(query=normalized_query)
            )
            quota = _format_public_post_search_quota(quota_result)
            if quota["remains"] <= 0 and not quota["query_is_free"]:
                return json.dumps(
                    {
                        "status": "quota_exhausted",
                        "message": "No free full-text public post searches remain.",
                        "quota": quota,
                        "stars_spent": 0,
                    },
                    ensure_ascii=False,
                )

        input_offset_peer = (
            await resolve_input_entity(offset_peer)
            if offset_peer is not None
            else types.InputPeerEmpty()
        )
        result = await client(
            functions.channels.SearchPostsRequest(
                query=normalized_query,
                hashtag=normalized_hashtag,
                offset_rate=offset_rate,
                offset_peer=input_offset_peer,
                offset_id=offset_id,
                limit=limit,
                allow_paid_stars=None,
            )
        )

        chat_by_id = {utils.get_peer_id(chat): chat for chat in getattr(result, "chats", [])}
        posts = []
        for message in getattr(result, "messages", []):
            peer_id = utils.get_peer_id(message.peer_id)
            chat = chat_by_id.get(peer_id)
            username = getattr(chat, "username", None) if chat else None

            post = format_message(message)
            post["channel"] = {
                "id": peer_id,
                "title": getattr(chat, "title", None) if chat else None,
                "username": username,
            }
            post["url"] = f"https://t.me/{username}/{message.id}" if username else None
            post["views"] = getattr(message, "views", None)
            post["forwards"] = getattr(message, "forwards", None)

            reactions = getattr(message, "reactions", None)
            reaction_results = getattr(reactions, "results", None) if reactions else None
            post["reactions"] = (
                sum(getattr(item, "count", 0) or 0 for item in reaction_results)
                if reaction_results
                else 0
            )
            posts.append(post)

        next_offset = None
        if posts:
            last_message = result.messages[-1]
            last_peer_id = utils.get_peer_id(last_message.peer_id)
            last_chat = chat_by_id.get(last_peer_id)
            last_username = getattr(last_chat, "username", None) if last_chat else None
            next_rate = getattr(result, "next_rate", None)
            if next_rate is None and getattr(last_message, "date", None):
                next_rate = int(last_message.date.timestamp())
            next_offset = {
                "offset_rate": next_rate or 0,
                "offset_peer": f"@{last_username}" if last_username else last_peer_id,
                "offset_id": last_message.id,
            }

        updated_quota = getattr(result, "search_flood", None)
        if updated_quota is not None:
            quota = _format_public_post_search_quota(updated_quota)

        return json.dumps(
            {
                "status": "ok",
                "mode": "query" if normalized_query else "hashtag",
                "query": normalized_query,
                "hashtag": normalized_hashtag,
                "count": getattr(result, "count", len(posts)),
                "inexact": bool(getattr(result, "inexact", False)),
                "returned": len(posts),
                "posts": posts,
                "clues": _extract_public_post_clues(posts),
                "next_offset": next_offset,
                "quota": quota,
                "stars_spent": 0,
            },
            ensure_ascii=False,
            default=json_serializer,
        )
    except Exception as e:
        return log_and_format_error(
            "search_public_posts",
            e,
            query=normalized_query,
            hashtag=normalized_hashtag,
            limit=limit,
            offset_rate=offset_rate,
            offset_peer=offset_peer,
            offset_id=offset_id,
        )


@mcp.tool(
    annotations=ToolAnnotations(title="Get Pinned Messages", openWorldHint=True, readOnlyHint=True)
)
@validate_id("chat_id")
async def get_pinned_messages(chat_id: Union[int, str]) -> str:
    """
    Get all pinned messages in a chat.
    """
    try:
        entity = await resolve_entity(chat_id)

        # Use correct filter based on Telethon version
        try:
            # Try newer Telethon approach
            from telethon.tl.types import InputMessagesFilterPinned

            messages = await client.get_messages(entity, filter=InputMessagesFilterPinned())
        except (ImportError, AttributeError):
            # Fallback - try without filter and manually filter pinned
            all_messages = await client.get_messages(entity, limit=50)
            messages = [m for m in all_messages if getattr(m, "pinned", False)]

        if not messages:
            return "No pinned messages found in this chat."

        lines = []
        for msg in messages:
            sender_name = get_sender_name(msg)
            reply_info = ""
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                reply_info = f" | reply to {msg.reply_to.reply_to_msg_id}"
            lines.append(
                f"ID: {msg.id} | {sender_name} | Date: {msg.date}{reply_info} | Message: {msg.message or '[Media/No text]'}"
            )

        return "\n".join(lines)
    except Exception as e:
        logger.exception(f"get_pinned_messages failed (chat_id={chat_id})")
        return log_and_format_error("get_pinned_messages", e, chat_id=chat_id)


# ============================================================================
# DRAFT MANAGEMENT TOOLS
# ============================================================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="Save Draft", openWorldHint=True, destructiveHint=False, idempotentHint=True
    )
)
@validate_id("chat_id")
async def save_draft(
    chat_id: Union[int, str],
    message: str,
    reply_to_msg_id: Optional[int] = None,
    no_webpage: bool = False,
) -> str:
    """
    Save a draft message to a chat or channel. The draft will appear in the Telegram
    app's input field when you open that chat, allowing you to review and send it manually.

    Args:
        chat_id: The chat ID or username/channel to save the draft to
        message: The draft message text. Empty string clears the draft.
        reply_to_msg_id: Optional message ID to reply to
        no_webpage: If True, disable link preview in the draft
    """
    try:
        peer = await resolve_input_entity(chat_id)

        # Build reply_to parameter if provided
        reply_to = None
        if reply_to_msg_id:
            from telethon.tl.types import InputReplyToMessage

            reply_to = InputReplyToMessage(reply_to_msg_id=reply_to_msg_id)

        await client(
            functions.messages.SaveDraftRequest(
                peer=peer,
                message=message,
                no_webpage=no_webpage,
                reply_to=reply_to,
            )
        )

        if not message:
            return f"Draft cleared from chat {chat_id}."
        return f"Draft saved to chat {chat_id}. Open the chat in Telegram to see and send it."
    except Exception as e:
        logger.exception(f"save_draft failed (chat_id={chat_id})")
        return log_and_format_error("save_draft", e, chat_id=chat_id)


@mcp.tool(annotations=ToolAnnotations(title="Get Drafts", openWorldHint=True, readOnlyHint=True))
async def get_drafts() -> str:
    """
    Get all draft messages across all chats.
    Returns a list of drafts with their chat info and message content.
    """
    try:
        result = await client(functions.messages.GetAllDraftsRequest())

        # The result contains updates with draft info
        drafts_info = []

        # GetAllDraftsRequest returns Updates object with updates array
        if hasattr(result, "updates"):
            for update in result.updates:
                if hasattr(update, "draft") and update.draft:
                    draft = update.draft
                    peer_id = None

                    # Extract peer ID based on type
                    if hasattr(update, "peer"):
                        peer = update.peer
                        if hasattr(peer, "user_id"):
                            peer_id = peer.user_id
                        elif hasattr(peer, "chat_id"):
                            peer_id = -peer.chat_id
                        elif hasattr(peer, "channel_id"):
                            peer_id = -1000000000000 - peer.channel_id

                    draft_data = {
                        "peer_id": peer_id,
                        "message": getattr(draft, "message", ""),
                        "date": (
                            draft.date.isoformat()
                            if hasattr(draft, "date") and draft.date
                            else None
                        ),
                        "no_webpage": getattr(draft, "no_webpage", False),
                        "reply_to_msg_id": (
                            draft.reply_to.reply_to_msg_id
                            if hasattr(draft, "reply_to") and draft.reply_to
                            else None
                        ),
                    }
                    drafts_info.append(draft_data)

        if not drafts_info:
            return "No drafts found."

        return json.dumps(
            {"drafts": drafts_info, "count": len(drafts_info)}, ensure_ascii=False, default=json_serializer
        )
    except Exception as e:
        logger.exception("get_drafts failed")
        return log_and_format_error("get_drafts", e)


# ============================================================================
# FOLDER MANAGEMENT TOOLS
# ============================================================================


@mcp.tool(annotations=ToolAnnotations(title="List Folders", openWorldHint=True, readOnlyHint=True))
async def list_folders() -> str:
    """
    Get all dialog folders (filters) with their IDs, names, and emoji.
    Returns a list of folders that can be used with other folder tools.
    """
    try:
        result = await client(functions.messages.GetDialogFiltersRequest())

        folders = []
        for f in result.filters:
            # Skip system default folder
            if isinstance(f, DialogFilterDefault):
                continue

            if isinstance(f, DialogFilter):
                # Handle title which can be str or TextWithEntities
                title = f.title
                if isinstance(title, TextWithEntities):
                    title = title.text
                folder_data = {
                    "id": f.id,
                    "title": title,
                    "emoticon": getattr(f, "emoticon", None),
                    "contacts": getattr(f, "contacts", False),
                    "non_contacts": getattr(f, "non_contacts", False),
                    "groups": getattr(f, "groups", False),
                    "broadcasts": getattr(f, "broadcasts", False),
                    "bots": getattr(f, "bots", False),
                    "exclude_muted": getattr(f, "exclude_muted", False),
                    "exclude_read": getattr(f, "exclude_read", False),
                    "exclude_archived": getattr(f, "exclude_archived", False),
                    "included_peers_count": len(getattr(f, "include_peers", [])),
                    "excluded_peers_count": len(getattr(f, "exclude_peers", [])),
                    "pinned_peers_count": len(getattr(f, "pinned_peers", [])),
                }
                folders.append(folder_data)

            elif isinstance(f, DialogFilterChatlist):
                # Shared folders use DialogFilterChatlist type
                title = f.title
                if isinstance(title, TextWithEntities):
                    title = title.text
                folder_data = {
                    "id": f.id,
                    "title": title,
                    "emoticon": getattr(f, "emoticon", None),
                    "type": "shared",
                    "included_peers_count": len(getattr(f, "include_peers", [])),
                    "pinned_peers_count": len(getattr(f, "pinned_peers", [])),
                }
                folders.append(folder_data)

        if not folders:
            return "No folders found. Create one with create_folder tool."

        return json.dumps(
            {"folders": folders, "count": len(folders)}, ensure_ascii=False, default=json_serializer
        )
    except Exception as e:
        logger.exception("list_folders failed")
        return log_and_format_error("list_folders", e, ErrorCategory.FOLDER)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Add Chat to Folder", openWorldHint=True, destructiveHint=True, idempotentHint=True
    )
)
@validate_id("chat_id")
async def add_chat_to_folder(
    folder_id: int, chat_id: Union[int, str], pinned: bool = False
) -> str:
    """
    Add a chat to an existing folder.

    Args:
        folder_id: The folder ID (get from list_folders)
        chat_id: Chat ID or username to add
        pinned: Pin the chat in this folder (default False)
    """
    try:
        # Get the folder
        result = await client(functions.messages.GetDialogFiltersRequest())

        target_folder = None
        for f in result.filters:
            if isinstance(f, (DialogFilter, DialogFilterChatlist)) and f.id == folder_id:
                target_folder = f
                break

        if not target_folder:
            return (
                f"Folder with ID {folder_id} not found. Use list_folders to see available folders."
            )

        # Resolve chat to input peer
        try:
            peer = await resolve_input_entity(chat_id)
        except Exception as e:
            return f"Failed to resolve chat '{chat_id}': {str(e)}"

        # Check if already included (idempotent)
        include_peers = list(getattr(target_folder, "include_peers", []))
        pinned_peers = list(getattr(target_folder, "pinned_peers", []))

        # Get peer ID for comparison
        peer_id = utils.get_peer_id(peer)
        already_included = any(utils.get_peer_id(p) == peer_id for p in include_peers)
        already_pinned = any(utils.get_peer_id(p) == peer_id for p in pinned_peers)

        if already_included and (not pinned or already_pinned):
            return f"Chat {chat_id} is already in folder {folder_id}."

        # Add to appropriate list
        if not already_included:
            include_peers.append(peer)
        if pinned and not already_pinned:
            pinned_peers.append(peer)

        # Update the folder (keep all original attributes)
        if isinstance(target_folder, DialogFilterChatlist):
            updated_filter = DialogFilterChatlist(
                id=target_folder.id,
                title=target_folder.title,
                emoticon=getattr(target_folder, "emoticon", None),
                pinned_peers=pinned_peers,
                include_peers=include_peers,
                title_noanimate=getattr(target_folder, "title_noanimate", None),
                color=getattr(target_folder, "color", None),
            )
        else:
            updated_filter = DialogFilter(
                id=target_folder.id,
                title=target_folder.title,
                emoticon=getattr(target_folder, "emoticon", None),
                pinned_peers=pinned_peers,
                include_peers=include_peers,
                exclude_peers=list(getattr(target_folder, "exclude_peers", [])),
                contacts=getattr(target_folder, "contacts", False),
                non_contacts=getattr(target_folder, "non_contacts", False),
                groups=getattr(target_folder, "groups", False),
                broadcasts=getattr(target_folder, "broadcasts", False),
                bots=getattr(target_folder, "bots", False),
                exclude_muted=getattr(target_folder, "exclude_muted", False),
                exclude_read=getattr(target_folder, "exclude_read", False),
                exclude_archived=getattr(target_folder, "exclude_archived", False),
                title_noanimate=getattr(target_folder, "title_noanimate", None),
                color=getattr(target_folder, "color", None),
            )

        await client(
            functions.messages.UpdateDialogFilterRequest(id=folder_id, filter=updated_filter)
        )

        return (
            f"Chat {chat_id} added to folder {folder_id}" + (" (pinned)" if pinned else "") + "."
        )
    except Exception as e:
        logger.exception(f"add_chat_to_folder failed (folder_id={folder_id}, chat_id={chat_id})")
        return log_and_format_error(
            "add_chat_to_folder", e, ErrorCategory.FOLDER, folder_id=folder_id, chat_id=chat_id
        )


# 内存上限（MB）。2026-10-23 有一份进程连着断线重连一天半，涨到 100G，
# 把 16G 的 Mac 拖死、交换文件吃掉 30G 硬盘。正常一份 90~150M。
MEMORY_LIMIT_MB = int(os.environ.get("TG_MCP_MEMORY_LIMIT_MB", "2048"))


def _footprint_mb() -> Optional[int]:
    """本进程真实内存占用（含被压缩、换出的部分），只在 macOS 上取得到。"""
    if sys.platform != "darwin":
        return None
    import ctypes

    buf = ctypes.create_string_buffer(256)
    if ctypes.CDLL(None).proc_pid_rusage(os.getpid(), 0, buf) != 0:
        return None
    # rusage_info_v0.ri_phys_footprint 在第 72 字节
    return int.from_bytes(buf.raw[72:80], "little") // 1048576


async def _heartbeat(interval: int = 120):
    """后台心跳：定期检查连接，断连时自动恢复"""
    while True:
        await asyncio.sleep(interval)
        mb = _footprint_mb()
        if mb and mb > MEMORY_LIMIT_MB:
            logger.error(f"Heartbeat: memory {mb}MB > {MEMORY_LIMIT_MB}MB, exiting")
            os._exit(1)
        try:
            if not client.is_connected():
                logger.warning("Heartbeat: client disconnected, reconnecting...")
                await client.connect()
                logger.info("Heartbeat: reconnected")
        except Exception as e:
            logger.error(f"Heartbeat reconnect failed: {e}")


# 启动阶段总超时（秒）。客户端握手 30s 就放弃了，超过这个数还没起来的进程
# 留着只会占用同一个 session string，把后来的进程一起卡死在 auth。
STARTUP_TIMEOUT = int(os.environ.get("TG_MCP_STARTUP_TIMEOUT", "45"))


async def _startup() -> None:
    print("Starting Telegram client...", file=sys.stderr)
    await client.start()

    # 验证连接有效
    me = await client.get_me()
    print(f"Logged in as: {me.first_name} (@{me.username or 'N/A'})", file=sys.stderr)

    # Warm entity cache
    print("Warming entity cache...", file=sys.stderr)
    await client.get_dialogs()


async def _main() -> None:
    heartbeat_task = None
    try:
        try:
            await asyncio.wait_for(_startup(), timeout=STARTUP_TIMEOUT)
        except asyncio.TimeoutError:
            print(
                f"Startup timed out after {STARTUP_TIMEOUT}s — exiting so this process "
                "does not hold the shared session. Check for other main.py instances.",
                file=sys.stderr,
            )
            os._exit(1)

        # 启动后台心跳
        heartbeat_task = asyncio.create_task(_heartbeat())

        print("Telegram client started. Running MCP server...", file=sys.stderr)
        if HTTP_MODE:
            await mcp.run_streamable_http_async()
        else:
            await mcp.run_stdio_async()
    except Exception as e:
        print(f"Error starting client: {e}", file=sys.stderr)
        if "AuthKeyDuplicated" in type(e).__name__ or "two different IP addresses" in str(e):
            # 同一串登录凭证被两个程序同时用，Telegram 服务器直接作废它。
            # 只能人工重登一次：cd ~/tg-mcp && uv run --with telethon python relogin.py
            print(
                "\n========================================\n"
                "Telegram 登录凭证已被服务器作废。\n"
                "原因：同一串凭证在两个地方同时使用。\n"
                "怎么修：在终端里跑一次（会给你手机发验证码）\n"
                "  cd ~/tg-mcp && uv run --with telethon python relogin.py\n"
                "规矩：这串凭证只给 Claude 用，别在 Codex 或别的机器上同时配。\n"
                "========================================\n",
                file=sys.stderr,
            )
        if isinstance(e, sqlite3.OperationalError) and "database is locked" in str(e):
            print(
                "Database lock detected. Please ensure no other instances are running.",
                file=sys.stderr,
            )
        sys.exit(1)
    finally:
        if heartbeat_task:
            heartbeat_task.cancel()
        try:
            await client.disconnect()
        except Exception:
            pass


def _shared_server_running() -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", HTTP_PORT)) == 0


def main() -> None:
    global HTTP_MODE
    HTTP_MODE = "--http" in sys.argv[1:]
    if HTTP_MODE:
        from mcp.server.transport_security import TransportSecuritySettings

        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = HTTP_PORT
        mcp.settings.stateless_http = True
        mcp.settings.log_level = "WARNING"
        logging.getLogger().setLevel(logging.WARNING)  # 常驻进程，daemon.log 别被每次调用刷满
        # 下面两类每天几十条、全是常态：Telegram 服务器定期断开再自动重连；
        # 无状态 HTTP 下请求已答完、SDK 收尾时报 ClosedResourceError 大段堆栈（10-24~28 共 81 次，调用都成功）。
        # ponytail: 直接压掉；升级 mcp 库修好这个 SDK 问题后可删第二行
        logging.getLogger("telethon").setLevel(logging.ERROR)
        logging.getLogger("mcp.server.streamable_http").setLevel(logging.CRITICAL)
        # 挡住网页借浏览器冒充本机来调（DNS rebinding）：只认本机 Host、不认任何网页 Origin
        mcp.settings.transport_security = TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*"], allowed_origins=[]
        )
    elif _shared_server_running():
        sys.exit(
            f"共用的 Telegram 连接已在 127.0.0.1:{HTTP_PORT} 运行，不再另开一份。"
            f"Claude 配置里请用 http://127.0.0.1:{HTTP_PORT}/mcp 连它。"
        )
    nest_asyncio.apply()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
