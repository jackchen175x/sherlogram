"""消息类工具：读消息、发消息（文字 / 文件 / 语音 / 贴纸 / GIF / 名片 / 投票）、改删转发置顶、表情、按钮、下载、草稿。"""

import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Union

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from telethon import functions, types, utils

from .app import client, mcp
from .common import (
    GIF_DOCS,
    ChatRef,
    ValidationError,
    clip,
    clues_tail,
    dump_json,
    get_engagement_info,
    get_provenance_info,
    get_sender_name,
    log_and_format_error,
    parse_date,
    readable_path,
    resolve_entity,
    validate_id,
    writable_path,
)



def _invalid(fn: str, msg: str, **kwargs) -> str:
    """校验错误。必须在 try 外面调：try 里抛出会被 except 重新包成看不出原因的通用报错。"""
    return log_and_format_error(fn, ValidationError(msg), prefix="VALIDATION-001", user_message=msg, **kwargs)


# ---------- 消息的一行摘要 ----------


def _button_rows(msg) -> List[list]:
    """消息上的键盘按钮，按行返回。

    原版先读 msg.buttons、没有再退回 reply_markup.rows；前者只是 Telethon 把后者包了一层，
    还要求消息绑着 client，直接读后者更简单也更稳。
    """
    rm = getattr(msg, "reply_markup", None)
    return [row.buttons for row in getattr(rm, "rows", None) or []]


def _media_info(msg) -> str:
    """有媒体时附一段摘要：类型、文件名、mime、大小。省掉一次单独查媒体的往返。"""
    if not msg.media:
        return ""
    parts = [type(msg.media).__name__.removeprefix("MessageMedia")]
    f = getattr(msg, "file", None)  # Telethon 只给照片 / 文件包一层 File，网页预览之类没有
    if f:
        parts += [p for p in (f.name, f.mime_type) if p]
        if f.size:
            parts.append(f"{f.size}B")
    return f" | media=[{', '.join(parts)}]"


def _buttons_info(msg) -> str:
    """有按钮时附按钮列表。网址按钮带上网址：卡网 / 下单链接常藏在按钮里，正文里没有。"""
    items = []
    for r, row in enumerate(_button_rows(msg)):
        for c, b in enumerate(row):
            kind = type(b).__name__.removeprefix("KeyboardButton").lower() or "text"
            item = {"row": r, "col": c, "text": b.text, "type": kind}
            if getattr(b, "url", None):
                item["url"] = b.url
            items.append(item)
    return f" | buttons={dump_json(items)}" if items else ""


def _line(msg, full_text: bool = False, mark: str = "") -> str:
    reply = f" | reply to {msg.reply_to.reply_to_msg_id}" if msg.reply_to and msg.reply_to.reply_to_msg_id else ""
    return (
        f"ID: {msg.id} | {get_sender_name(msg)} | Date: {msg.date}{mark}{reply}"
        f"{get_provenance_info(msg)}{get_engagement_info(msg)}{_media_info(msg)}{_buttons_info(msg)}"
        f" | Message: {clip(msg.message, full_text) or '[Media/No text]'}"
    )


# ---------- 读 ----------


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="List Messages", openWorldHint=True, readOnlyHint=True))
@validate_id("chat_id", "from_user")
async def list_messages(
    chat_id: ChatRef,
    limit: int = 20,
    search_query: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    from_user: Optional[ChatRef] = None,
    full_text: bool = False,
    around_message_id: Optional[int] = None,
) -> str:
    """Read one chat: latest N, keyword search, date range, one sender, or context around a message.

    Args:
        chat_id: Chat ID or @username.
        limit: Max messages.
        search_query: Only messages containing this text.
        from_date / to_date: Date bounds, YYYY-MM-DD, both inclusive.
        from_user: Only this sender (ID or @username); searching their @handle does NOT find their ads.
        full_text: Don't clip bodies (default clips at 400 chars and says how much was cut).
        around_message_id: Return limit//2 messages before and after this one (other filters ignored).
    """
    try:
        from_dt, to_dt = parse_date(from_date), parse_date(to_date, end_of_day=True)
    except ValueError:
        return _invalid("list_messages", "Invalid date format. Use YYYY-MM-DD.", from_date=from_date, to_date=to_date)
    try:
        entity = await resolve_entity(chat_id)
        if around_message_id:
            half = max(limit // 2, 1)
            center = await client.get_messages(entity, ids=around_message_id)
            if center is None:
                return f"Message {around_message_id} not found in chat {chat_id}."
            before = await client.get_messages(entity, limit=half, max_id=around_message_id)
            after = await client.get_messages(entity, limit=half, min_id=around_message_id, reverse=True)
            messages = sorted([*before, center, *after], key=lambda m: m.id)
            lines = [_line(m, full_text, " [THIS MESSAGE]" if m.id == around_message_id else "") for m in messages]
            return "\n".join(lines) + clues_tail(messages)

        params = {}
        if from_user:
            # 走 resolve_entity 而不是直接透传：它带进程内缓存，批量按卖家聚合时
            # 不会对同一个 handle 反复发 ResolveUsernameRequest（该请求是已知的封号源）。
            params["from_user"] = await resolve_entity(from_user)
        messages = []
        if search_query:
            # 搜索不能和 offset_date 一起用：只让服务器搜关键词，日期边界在本地卡
            async for msg in client.iter_messages(entity, search=search_query, **params):  # 新 → 旧
                if to_dt and msg.date > to_dt:
                    continue
                if from_dt and msg.date < from_dt:
                    break
                messages.append(msg)
                if len(messages) >= limit:
                    break
        elif from_dt:
            # 有起始日期：从起始日往后走（旧 → 新），服务器按日期直接定位，不多拉
            async for msg in client.iter_messages(entity, offset_date=from_dt, reverse=True, **params):
                if to_dt and msg.date > to_dt:
                    break
                if msg.date < from_dt:
                    continue
                messages.append(msg)
                if len(messages) >= limit:
                    break
        elif to_dt:
            # 只有截止日期：从截止点往前走。offset_date 不含边界，+1 微秒让 to_date 当天算进去
            async for msg in client.iter_messages(entity, offset_date=to_dt + timedelta(microseconds=1), **params):
                messages.append(msg)
                if len(messages) >= limit:
                    break
        else:
            messages = await client.get_messages(entity, limit=limit, **params)

        if not messages:
            return "No messages found matching the criteria."
        return "\n".join(_line(m, full_text) for m in messages) + clues_tail(messages)
    except Exception as e:
        return log_and_format_error("list_messages", e, chat_id=chat_id)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Get Pinned Messages", openWorldHint=True, readOnlyHint=True))
@validate_id("chat_id")
async def get_pinned_messages(chat_id: ChatRef) -> str:
    """List pinned messages in a chat (full text, up to 50).

    Args:
        chat_id: Chat ID or @username.
    """
    try:
        entity = await resolve_entity(chat_id)
        # 必须写 limit：Telethon 的 get_messages 不给 limit 默认只取 1 条，现版因此只返回过最新那条置顶
        messages = await client.get_messages(entity, limit=50, filter=types.InputMessagesFilterPinned())
        if not messages:
            return "No pinned messages found in this chat."
        # 置顶多是规则 / 价目表，信息最密，不截断
        return "\n".join(_line(m, full_text=True) for m in messages)
    except Exception as e:
        return log_and_format_error("get_pinned_messages", e, chat_id=chat_id)


# ---------- 发 ----------


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Send Message", openWorldHint=True, readOnlyHint=False, destructiveHint=False))
@validate_id("chat_id")
async def send_message(
    chat_id: ChatRef,
    message: str = "",
    reply_to: Optional[int] = None,
    parse_mode: Optional[str] = None,
    file_path: Optional[str] = None,
    as_voice: bool = False,
    gif_id: Optional[int] = None,
    ctx: Optional[Context] = None,
) -> str:
    """Send text, a file (.webp goes as sticker), a voice note or a saved GIF; message becomes the caption.

    Args:
        chat_id: Chat ID or @username.
        message: Text, or caption when sending a file/GIF.
        reply_to: Message ID to reply to.
        parse_mode: 'html', 'md', or omit for plain text.
        file_path: Local file under allowed roots.
        as_voice: Send file_path (.ogg/.opus) as a voice note.
        gif_id: Document ID of a GIF in your saved GIFs.
    """
    if not (message or file_path or gif_id):
        return _invalid("send_message", "Provide message, file_path or gif_id.", chat_id=chat_id)
    file = None
    if gif_id:
        # 原版 send_gif 把裸数字丢给 send_file，Telethon 直接报 "Cannot use 123 as file"：
        # 发文档要带 access_hash + file_reference。先查 search_gifs 存下的文档，再查已收藏的 GIF。
        file = GIF_DOCS.get(gif_id)
        if file is None:
            saved = await client(functions.messages.GetSavedGifsRequest(hash=0))
            file = next((d for d in getattr(saved, "gifs", None) or [] if d.id == gif_id), None)
        if file is None:
            return _invalid("send_message", f"GIF {gif_id} not found: run search_gifs first, or save it in Telegram.", gif_id=gif_id)
    elif file_path:
        kind = "voice" if as_voice else "sticker" if file_path.lower().endswith(".webp") else "file"
        path, err = await readable_path(file_path, ctx, kind)
        if err:
            raise RuntimeError(err)
        file = str(path)
    try:
        entity = await resolve_entity(chat_id)
        if file is None:
            sent = await client.send_message(entity, message, reply_to=reply_to, parse_mode=parse_mode)
        else:
            sent = await client.send_file(
                entity, file, caption=message or None, reply_to=reply_to, parse_mode=parse_mode, voice_note=as_voice
            )
        return f"Sent message {sent.id} to chat {chat_id}."
    except Exception as e:
        return log_and_format_error("send_message", e, chat_id=chat_id, file_path=file_path, gif_id=gif_id)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Send Contact", openWorldHint=True, readOnlyHint=False, destructiveHint=False))
@validate_id("chat_id")
async def send_contact(chat_id: ChatRef, phone: str, first_name: str, last_name: str = "", vcard: str = "") -> str:
    """Send a contact card to a chat.

    Args:
        chat_id: Chat ID or @username.
        phone: Contact's phone number.
        first_name / last_name: Contact's name.
        vcard: Extra vCard data.
    """
    try:
        entity = await resolve_entity(chat_id)
        media = types.InputMediaContact(phone_number=phone, first_name=first_name, last_name=last_name, vcard=vcard)
        sent = await client.send_file(entity, media)
        return f"Contact sent to chat {chat_id} (message {sent.id})."
    except Exception as e:
        return log_and_format_error("send_contact", e, chat_id=chat_id)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Create Poll", openWorldHint=True, readOnlyHint=False, destructiveHint=False))
@validate_id("chat_id")
async def create_poll(
    chat_id: ChatRef,
    question: str,
    options: List[str],
    multiple_choice: bool = False,
    quiz_mode: bool = False,
    public_votes: bool = False,
    close_date: Optional[str] = None,
    correct_option: Optional[int] = None,
) -> str:
    """Create a native Telegram poll or quiz.

    Args:
        chat_id: Chat ID or @username.
        question: Poll question. options: 2-10 answers.
        multiple_choice: Allow several answers. public_votes: Show who voted (not allowed in channels).
        quiz_mode: Quiz with one right answer; requires correct_option (0-based index).
        close_date: ISO datetime, e.g. 2026-11-01 18:00 (local time unless an offset is given).
    """
    if not 2 <= len(options) <= 10:
        return _invalid("create_poll", "Poll must have 2-10 options.", options=options)
    # Telegram 规定测验必须带正确答案（否则报 QUIZ_CORRECT_ANSWERS_EMPTY），原版没传，测验模式从来发不出去
    if quiz_mode and (correct_option is None or not 0 <= correct_option < len(options)):
        return _invalid("create_poll", "quiz_mode needs correct_option (0-based index into options).", correct_option=correct_option)
    try:
        close_dt = datetime.fromisoformat(close_date.replace("Z", "+00:00")) if close_date else None
    except ValueError:
        return _invalid("create_poll", "Invalid close_date. Use ISO format like 2026-11-01 18:00.", close_date=close_date)
    try:
        entity = await resolve_entity(chat_id)
        poll = types.Poll(
            id=random.randint(0, 2**63 - 1),
            question=types.TextWithEntities(text=question, entities=[]),
            answers=[types.PollAnswer(text=types.TextWithEntities(text=o, entities=[]), option=bytes([i])) for i, o in enumerate(options)],
            multiple_choice=multiple_choice,
            quiz=quiz_mode,
            public_voters=public_votes,
            close_date=close_dt,
        )
        correct = [bytes([correct_option])] if quiz_mode else None
        sent = await client.send_file(entity, types.InputMediaPoll(poll=poll, correct_answers=correct))
        return f"Poll created in chat {chat_id} (message {sent.id})."
    except Exception as e:
        return log_and_format_error("create_poll", e, chat_id=chat_id, question=question)


# ---------- 改 / 删 / 转发 / 置顶 / 已读 ----------


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Edit Message", openWorldHint=True, readOnlyHint=False, destructiveHint=True))
@validate_id("chat_id")
async def edit_message(chat_id: ChatRef, message_id: int, new_text: str) -> str:
    """Replace the text of a message you sent.

    Args:
        chat_id: Chat ID or @username.
        message_id: Message to edit.
        new_text: New text.
    """
    try:
        entity = await resolve_entity(chat_id)
        await client.edit_message(entity, message_id, new_text)
        return f"Message {message_id} edited."
    except Exception as e:
        return log_and_format_error("edit_message", e, chat_id=chat_id, message_id=message_id)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Delete Message", openWorldHint=True, readOnlyHint=False, destructiveHint=True))
@validate_id("chat_id")
async def delete_message(chat_id: ChatRef, message_ids: Union[int, List[int]]) -> str:
    """Delete one or more messages (for everyone, where allowed).

    Args:
        chat_id: Chat ID or @username.
        message_ids: One message ID or a list.
    """
    ids = message_ids if isinstance(message_ids, list) else [message_ids]
    try:
        entity = await resolve_entity(chat_id)
        results = await client.delete_messages(entity, ids)
        # pts_count 是服务器真正删掉的条数：没权限删别人的消息时不报错，只是这里是 0
        deleted = sum(getattr(r, "pts_count", 0) for r in results or [])
        return f"Deleted {deleted} of {len(ids)} message(s) in chat {chat_id}."
    except Exception as e:
        return log_and_format_error("delete_message", e, chat_id=chat_id, message_ids=message_ids)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Forward Message", openWorldHint=True, readOnlyHint=False, destructiveHint=False))
@validate_id("from_chat_id", "to_chat_id")
async def forward_message(from_chat_id: ChatRef, message_ids: Union[int, List[int]], to_chat_id: ChatRef) -> str:
    """Forward one or more messages to another chat.

    Args:
        from_chat_id: Source chat ID or @username.
        message_ids: One message ID or a list.
        to_chat_id: Destination chat ID or @username.
    """
    ids = message_ids if isinstance(message_ids, list) else [message_ids]
    try:
        from_entity = await resolve_entity(from_chat_id)
        to_entity = await resolve_entity(to_chat_id)
        await client.forward_messages(to_entity, ids, from_entity)
        return f"Forwarded {len(ids)} message(s) from {from_chat_id} to {to_chat_id}."
    except Exception as e:
        return log_and_format_error("forward_message", e, from_chat_id=from_chat_id, to_chat_id=to_chat_id)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Pin Message", openWorldHint=True, readOnlyHint=False, destructiveHint=False))
@validate_id("chat_id")
async def pin_message(chat_id: ChatRef, message_id: int, unpin: bool = False) -> str:
    """Pin a message in a chat, or unpin it.

    Args:
        chat_id: Chat ID or @username.
        message_id: Message to pin/unpin.
        unpin: Unpin instead of pin.
    """
    try:
        entity = await resolve_entity(chat_id)
        if unpin:
            await client.unpin_message(entity, message_id)
        else:
            await client.pin_message(entity, message_id)
        return f"Message {message_id} {'unpinned' if unpin else 'pinned'} in chat {chat_id}."
    except Exception as e:
        return log_and_format_error("pin_message", e, chat_id=chat_id, message_id=message_id)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Mark As Read", openWorldHint=True, readOnlyHint=False, destructiveHint=False))
@validate_id("chat_id")
async def mark_as_read(chat_id: ChatRef) -> str:
    """Mark all messages in a chat as read.

    Args:
        chat_id: Chat ID or @username.
    """
    try:
        entity = await resolve_entity(chat_id)
        await client.send_read_acknowledge(entity)
        return f"Marked all messages as read in chat {chat_id}."
    except Exception as e:
        return log_and_format_error("mark_as_read", e, chat_id=chat_id)


# ---------- 表情 / 按钮 ----------


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="React", openWorldHint=True, readOnlyHint=False, destructiveHint=False))
@validate_id("chat_id")
async def react(
    chat_id: ChatRef, message_id: int, action: str = "add", emoji: Optional[str] = None, big: bool = False, limit: int = 50
) -> str:
    """Add or remove your emoji reaction on a message, or list who reacted with what.

    Args:
        chat_id: Chat ID or @username.
        message_id: Target message.
        action: One of: add, remove, list.
        emoji: Emoji for add, e.g. 👍 ❤️ 🔥. big: Big animation.
        limit: Max reactions for list (not available in broadcast channels).
    """
    if action not in ("add", "remove", "list"):
        return _invalid("react", f"Unknown action: {action}. Use one of: add, remove, list.", action=action)
    if action == "add" and not emoji:
        return _invalid("react", "action=add needs emoji.", action=action)
    try:
        entity = await resolve_entity(chat_id)
        if action == "list":
            result = await client(functions.messages.GetMessageReactionsListRequest(peer=entity, id=message_id, limit=limit))
            items = []
            for r in result.reactions:
                re_ = r.reaction
                emo = getattr(re_, "emoticon", None) or (f"custom:{re_.document_id}" if hasattr(re_, "document_id") else None)
                items.append({"peer_id": utils.get_peer_id(r.peer_id), "emoji": emo, "date": r.date})
            return dump_json({"message_id": message_id, "count": len(items), "reactions": items})
        # 空列表 = 撤掉自己的表情
        reaction = [types.ReactionEmoji(emoticon=emoji)] if action == "add" else []
        await client(functions.messages.SendReactionRequest(peer=entity, msg_id=message_id, big=big, reaction=reaction))
        return f"Reaction {'added' if action == 'add' else 'removed'} on message {message_id} in chat {chat_id}."
    except Exception as e:
        return log_and_format_error("react", e, chat_id=chat_id, message_id=message_id, action=action)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Press Inline Button", openWorldHint=True, readOnlyHint=False, destructiveHint=True))
@validate_id("chat_id")
async def press_inline_button(
    chat_id: ChatRef, message_id: Optional[int] = None, button_text: Optional[str] = None, button_index: Optional[int] = None
) -> str:
    """Press an inline callback button on a bot message (list_messages shows buttons).

    Args:
        chat_id: Chat or bot ID / @username.
        message_id: Message with the buttons; omitted = newest of the last 20 messages that has buttons.
        button_text: Exact button text (case-insensitive).
        button_index: 0-based index, counting left-to-right, top-to-bottom.
    """
    if button_text is None and button_index is None:
        return _invalid("press_inline_button", "Provide button_text or button_index.", chat_id=chat_id)
    try:
        entity = await resolve_entity(chat_id)
        if message_id is not None:
            target = await client.get_messages(entity, ids=message_id)
            if target and not _button_rows(target):
                # 原版踩过的坑：按编号取消息时 Telethon 有时不带 reply_markup，再从最近消息里找同一条
                recent = await client.get_messages(entity, limit=30)
                target = next((m for m in recent if m.id == target.id and _button_rows(m)), target)
        else:
            recent = await client.get_messages(entity, limit=20)
            target = next((m for m in recent if _button_rows(m)), None)
        if not target:
            return "No message with inline buttons found. Specify message_id to target a specific message."
        buttons = [b for row in _button_rows(target) for b in row]
        if not buttons:
            return f"Message {target.id} does not contain inline buttons."

        button = None
        if button_text:
            wanted = button_text.strip().lower()
            button = next((b for b in buttons if (b.text or "").strip().lower() == wanted), None)
        if button is None and button_index is not None:
            if not 0 <= button_index < len(buttons):
                return f"button_index out of range. Valid indices: 0-{len(buttons) - 1}."
            button = buttons[button_index]
        if button is None:
            available = ", ".join(f"[{i}] {b.text or '<no text>'}" for i, b in enumerate(buttons))
            return f"Button not found. Available buttons: {available}"

        data = getattr(button, "data", None)
        if not data:
            url = getattr(button, "url", None)
            return f"Selected button opens a URL instead of sending a callback: {url}" if url else "Selected button has no callback data to press."
        answer = await client(functions.messages.GetBotCallbackAnswerRequest(peer=entity, msg_id=target.id, data=data))
        parts = [p for p in (getattr(answer, "message", None), getattr(answer, "url", None)) if p]
        if getattr(answer, "alert", None):
            parts.append("(shown as an alert)")
        return " ".join(parts) or "Button pressed successfully."
    except Exception as e:
        return log_and_format_error("press_inline_button", e, chat_id=chat_id, message_id=message_id, button_text=button_text)


# ---------- 下载 / 草稿 ----------


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Download Media", openWorldHint=True, readOnlyHint=False, destructiveHint=False))
@validate_id("chat_id")
async def download_media(chat_id: ChatRef, message_id: int, file_path: Optional[str] = None, ctx: Optional[Context] = None) -> str:
    """Download a message's photo/file to disk under allowed roots; returns the saved path.

    Args:
        chat_id: Chat ID or @username.
        message_id: Message with the media.
        file_path: Target path; omitted = <first allowed root>/downloads/. The extension follows the real file type.
    """
    out, err = await writable_path(file_path, f"telegram_{chat_id}_{message_id}_{int(time.time())}", ctx)
    if err:
        raise RuntimeError(err)
    try:
        entity = await resolve_entity(chat_id)
        msg = await client.get_messages(entity, ids=message_id)
        if not msg or not msg.media:
            return "No media found in the specified message."
        # 去掉用户给的后缀，让 Telethon 按真实内容补后缀：传 ticket.jpg 而实际是 PDF 时，
        # Telethon 会照写 ticket.jpg，打开就坏
        downloaded = await client.download_media(msg, file=str(out.with_suffix("")))
        if not downloaded:
            return f"Download failed for message {message_id}."
        final = Path(downloaded).resolve()
        if out.parent not in final.parents:  # Telethon 最终落点要再核一次，别写出白名单
            raise RuntimeError(f"Downloaded file landed outside allowed roots: {final}")
        return f"Media downloaded to {final}."
    except Exception as e:
        return log_and_format_error("download_media", e, chat_id=chat_id, message_id=message_id, file_path=file_path)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Save Draft", openWorldHint=True, readOnlyHint=False, destructiveHint=False, idempotentHint=True))
@validate_id("chat_id")
async def save_draft(chat_id: ChatRef, message: str, reply_to_msg_id: Optional[int] = None, no_webpage: bool = False) -> str:
    """Save a draft in a chat's input box for you to review and send in the app; empty message clears it.

    Args:
        chat_id: Chat ID or @username.
        message: Draft text ("" clears the draft).
        reply_to_msg_id: Message the draft replies to.
        no_webpage: Disable link preview.
    """
    try:
        entity = await resolve_entity(chat_id)
        reply_to = types.InputReplyToMessage(reply_to_msg_id=reply_to_msg_id) if reply_to_msg_id else None
        await client(functions.messages.SaveDraftRequest(peer=entity, message=message, no_webpage=no_webpage, reply_to=reply_to))
        if not message:
            return f"Draft cleared from chat {chat_id}."
        return f"Draft saved to chat {chat_id}. Open the chat in Telegram to see and send it."
    except Exception as e:
        return log_and_format_error("save_draft", e, chat_id=chat_id)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Get Drafts", openWorldHint=True, readOnlyHint=True))
async def get_drafts() -> str:
    """List your unsent drafts across all chats."""
    try:
        result = await client(functions.messages.GetAllDraftsRequest())
        drafts = []
        # 返回的是一包 Updates，每个 UpdateDraftMessage 是一个聊天的草稿
        for u in getattr(result, "updates", None) or []:
            draft = getattr(u, "draft", None)
            if not draft or isinstance(draft, types.DraftMessageEmpty):
                continue
            drafts.append({
                "peer_id": utils.get_peer_id(u.peer),
                "message": draft.message,
                "date": draft.date,
                "no_webpage": bool(getattr(draft, "no_webpage", False)),
                "reply_to_msg_id": getattr(getattr(draft, "reply_to", None), "reply_to_msg_id", None),
            })
        if not drafts:
            return "No drafts found."
        return dump_json({"drafts": drafts, "count": len(drafts)})
    except Exception as e:
        return log_and_format_error("get_drafts", e)
