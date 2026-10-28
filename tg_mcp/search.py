"""搜索类工具：全局消息、公开聊天、公开频道帖子（不花 Stars）、GIF。

另外放了 list_messages 也要用的几个小件（_clip / _clues_tail / MSG_CLIP），
以及 market_watch.py 要用的 extract_public_post_clues。
"""

from datetime import datetime
from typing import Any, Dict, Optional

from mcp.types import ToolAnnotations
from telethon import functions, types, utils

from .app import client, mcp
from .common import (
    ChatRef,
    GIF_DOCS,
    clip,
    clues_tail,
    dump_json,
    extract_public_post_clues,
    format_entity,
    format_message,
    get_provenance_info,
    get_sender_name,
    log_and_format_error,
    resolve_input_entity,
    validate_id,
)

# ---------- 工具 ----------


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Search Public Chats", openWorldHint=True, readOnlyHint=True))
async def search_public_chats(query: str, limit: int = 20) -> str:
    """Search public chats, channels and bots by username or title.

    Args:
        query: Username or title words.
        limit: Max results.
    """
    try:
        result = await client(functions.contacts.SearchRequest(q=query, limit=limit))
        return dump_json([format_entity(e) for e in result.chats + result.users])
    except Exception as e:
        return log_and_format_error("search_public_chats", e, query=query, limit=limit)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Search Global Messages", openWorldHint=True, readOnlyHint=True))
async def search_global(query: str, page: int = 1, page_size: int = 20) -> str:
    """Search messages across the chats this account can see (Telegram's legacy global search).

    To search channels you have not joined, use search_public_posts instead.

    Args:
        query: Search words.
        page: Page number, starting at 1.
        page_size: Messages per page.
    """
    try:
        messages = await client.get_messages(None, limit=page_size, search=query, add_offset=(page - 1) * page_size)
        if not messages:
            return "No messages found for this page."

        lines = []
        for msg in messages:
            chat = msg.chat
            chat_name = getattr(chat, "title", None) or getattr(chat, "first_name", "") or str(msg.chat_id)
            chat_username = getattr(chat, "username", None)
            chat_label = f"{chat_name} (@{chat_username})" if chat_username else chat_name
            lines.append(
                f"Chat: {chat_label} | ID: {msg.id} | {get_sender_name(msg)} | "
                f"Date: {msg.date}{get_provenance_info(msg)} | Message: {clip(msg.message)}"
            )
        return "\n".join(lines) + clues_tail(messages)
    except Exception as e:
        return log_and_format_error("search_global", e, query=query, page=page, page_size=page_size)


def _format_public_post_search_quota(search_flood: Any) -> Dict[str, Any]:
    """把 Telegram 的 SearchPostsFlood（每日免费搜索额度）转成可输出的 JSON。"""
    wait_till = getattr(search_flood, "wait_till", None)
    wait_till_iso = datetime.fromtimestamp(wait_till).astimezone().isoformat() if wait_till else None
    return {
        "total_daily": getattr(search_flood, "total_daily", 0),
        "remains": getattr(search_flood, "remains", 0),
        "query_is_free": bool(getattr(search_flood, "query_is_free", False)),
        "wait_till": wait_till,
        "wait_till_iso": wait_till_iso,
        "stars_required": getattr(search_flood, "stars_amount", 0),
    }


def _invalid(error: str) -> str:
    """参数不合格：按这个工具一贯的做法返回 JSON（不抛异常），调用方能直接读到原因。"""
    return dump_json({"status": "invalid_request", "error": error})


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Search Public Posts (No Stars)", openWorldHint=True, readOnlyHint=True))
@validate_id("offset_peer")
async def search_public_posts(
    query: Optional[str] = None,
    hashtag: Optional[str] = None,
    limit: int = 20,
    offset_rate: int = 0,
    offset_peer: Optional[ChatRef] = None,
    offset_id: int = 0,
) -> str:
    """Search posts of all public channels, including ones you have not joined, never paying Stars.

    Give exactly one of query / hashtag. Full-text search uses Telegram's small daily free quota (checked first); hashtag search and later pages are free.

    Args:
        query: Full-text search words.
        hashtag: Hashtag to search, with or without '#'.
        limit: Posts per page, 1-100.
        offset_rate: next_offset.offset_rate from the previous page.
        offset_peer: next_offset.offset_peer from the previous page.
        offset_id: next_offset.offset_id from the previous page.
    """
    normalized_query = query.strip() if query else None
    normalized_hashtag = hashtag.strip().lstrip("#") if hashtag else None

    if bool(normalized_query) == bool(normalized_hashtag):
        return _invalid("Provide exactly one of query or hashtag.")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        return _invalid("limit must be between 1 and 100.")
    if offset_rate < 0 or offset_id < 0:
        return _invalid("offset_rate and offset_id must be non-negative.")
    if (offset_rate or offset_id) and offset_peer is None:
        return _invalid("offset_peer is required when continuing a paginated search.")

    try:
        quota = None
        is_first_page = offset_rate == 0 and offset_id == 0 and offset_peer is None
        # 全文搜索每天免费次数很少，用完再搜 Telegram 会要 Stars；先查额度，没有就停，绝不授权付费。
        # 翻页（next_offset）和话题搜索不占全文额度。
        if normalized_query and is_first_page:
            quota_result = await client(functions.channels.CheckSearchPostsFloodRequest(query=normalized_query))
            quota = _format_public_post_search_quota(quota_result)
            if quota["remains"] <= 0 and not quota["query_is_free"]:
                return dump_json(
                    {
                        "status": "quota_exhausted",
                        "message": "No free full-text public post searches remain.",
                        "quota": quota,
                        "stars_spent": 0,
                    }
                )

        input_offset_peer = await resolve_input_entity(offset_peer) if offset_peer is not None else types.InputPeerEmpty()
        result = await client(
            functions.channels.SearchPostsRequest(
                query=normalized_query,
                hashtag=normalized_hashtag,
                offset_rate=offset_rate,
                offset_peer=input_offset_peer,
                offset_id=offset_id,
                limit=limit,
                allow_paid_stars=None,  # 明确不授权付费
            )
        )

        chat_by_id = {utils.get_peer_id(chat): chat for chat in getattr(result, "chats", [])}
        posts = []
        for message in getattr(result, "messages", []):
            peer_id = utils.get_peer_id(message.peer_id)
            chat = chat_by_id.get(peer_id)
            username = getattr(chat, "username", None) if chat else None

            post = format_message(message)
            post["channel"] = {"id": peer_id, "title": getattr(chat, "title", None) if chat else None, "username": username}
            post["url"] = f"https://t.me/{username}/{message.id}" if username else None
            post["views"] = getattr(message, "views", None)
            post["forwards"] = getattr(message, "forwards", None)
            reaction_results = getattr(getattr(message, "reactions", None), "results", None)
            post["reactions"] = sum(getattr(item, "count", 0) or 0 for item in reaction_results) if reaction_results else 0
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

        return dump_json(
            {
                "status": "ok",
                "mode": "query" if normalized_query else "hashtag",
                "query": normalized_query,
                "hashtag": normalized_hashtag,
                "count": getattr(result, "count", len(posts)),
                "inexact": bool(getattr(result, "inexact", False)),
                "returned": len(posts),
                "posts": posts,
                "clues": extract_public_post_clues(posts),
                "next_offset": next_offset,
                "quota": quota,
                "stars_spent": 0,
            }
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


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Search GIFs", openWorldHint=True, readOnlyHint=True))
async def search_gifs(query: str, limit: int = 10) -> str:
    """Search GIFs via the @gif bot; returns document IDs for send_message(gif_id=...).

    Args:
        query: Search words.
        limit: Max GIFs.
    """
    try:
        # 原版在跟 @gif 机器人的聊天记录里搜，只能搜到自己发过的；这里直接用它的内联搜索，
        # 搜到的完整文档存进 GIF_DOCS，send_message(gif_id=...) 才发得出去（裸编号发不了）。
        res = await client(
            functions.messages.GetInlineBotResultsRequest(
                bot=await client.get_input_entity("gif"),
                peer=await client.get_input_entity("me"),
                query=query,
                offset="",
            )
        )
        docs = [r.document for r in getattr(res, "results", None) or [] if getattr(r, "document", None)][:limit]
        GIF_DOCS.update({d.id: d for d in docs})
        return dump_json([d.id for d in docs])
    except Exception as e:
        return log_and_format_error("search_gifs", e, query=query, limit=limit)
