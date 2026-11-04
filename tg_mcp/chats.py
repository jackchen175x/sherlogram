"""聊天层面的工具：列聊天、看群/人详情、成员、话题、建群改群、成员管理、邀请链接、加入退出、静音归档。"""

import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Union

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from telethon import errors, functions, types, utils
from telethon.tl.types import Channel, Chat, User

from .app import client, logger, mcp
from .common import (ChatRef, ValidationError, dump_json, format_entity, get_entity_filter_type, get_entity_type,
                     get_sender_name, log_and_format_error, readable_path, resolve_entity, validate_id)


def _unknown(name: str, value: str, choices) -> ValidationError:
    return ValidationError(f"Unknown {name}: {value}. Use one of: {', '.join(choices)}.")


def _full_request(entity):
    # 普通群（Chat）和超级群/频道（Channel）拿完整资料是两个接口
    if isinstance(entity, Chat):
        return functions.messages.GetFullChatRequest(chat_id=entity.id)
    return functions.channels.GetFullChannelRequest(entity)


async def _try_request(request, chat_id):
    """get_chat 的附加信息拿不到，不该让整个调用失败：记 warning，返回 None。"""
    try:
        return await client(request)
    except Exception as e:
        logger.warning(f"{type(request).__name__} failed for {chat_id}: {e}")
        return None


_STATUS = {
    types.UserStatusOnline: "online",
    types.UserStatusRecently: "recently",
    types.UserStatusLastWeek: "last week",
    types.UserStatusLastMonth: "last month",
}


def _status_text(status) -> str:
    # 卖家号「一个月没上线」本身就是跑路信号；对方隐藏精确时间时 Telegram 只给模糊档位
    if isinstance(status, types.UserStatusOffline) and status.was_online:
        return f"offline, last seen {status.was_online:%Y-%m-%d %H:%M} UTC"
    return _STATUS.get(type(status), "long ago or hidden")


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="List Chats", openWorldHint=True, readOnlyHint=True))
async def list_chats(
    chat_type: Optional[str] = None, limit: int = 20, unread_only: bool = False, unmuted_only: bool = False
) -> str:
    """List your chats with type, unread count and mute status.

    Args:
        chat_type: Filter: user, group or channel. Omit for all.
        limit: Dialogs fetched before filtering, so filters may return fewer.
        unread_only: Only chats with unread messages (or marked unread).
        unmuted_only: Only unmuted chats.
    """
    try:
        lines = []
        for dialog in await client.get_dialogs(limit=limit):
            entity = dialog.entity
            if chat_type and get_entity_filter_type(entity) != chat_type.lower():
                continue
            inner = getattr(dialog, "dialog", None)
            unread = getattr(dialog, "unread_count", 0) or 0
            # unread_mark 是手动「标为未读」，unread_count 为 0 时也算未读
            marked = bool(getattr(inner, "unread_mark", False))
            mute_until = getattr(getattr(inner, "notify_settings", None), "mute_until", None)
            if isinstance(mute_until, datetime):
                mute_until = mute_until.timestamp()
            muted = mute_until is not None and mute_until > time.time()
            if (unmuted_only and muted) or (unread_only and not unread and not marked):
                continue
            info = f"Chat ID: {entity.id}"
            if hasattr(entity, "title"):
                info += f", Title: {entity.title}"
            elif hasattr(entity, "first_name"):
                info += ", Name: " + " ".join(p for p in (entity.first_name, getattr(entity, "last_name", None)) if p)
            info += f", Type: {get_entity_type(entity)}"
            if getattr(entity, "username", None):
                info += f", Username: @{entity.username}"
            info += f", Unread: {unread}" if unread else (", Unread: marked" if marked else ", No unread messages")
            info += f", Muted: {'yes' if muted else 'no'}"
            mentions = getattr(dialog, "unread_mentions_count", 0) or 0
            if mentions:
                info += f", Unread mentions: {mentions}"
            lines.append(info)
        return "\n".join(lines) if lines else "No chats found matching the criteria."
    except Exception as e:
        return log_and_format_error("list_chats", e, chat_type=chat_type, limit=limit)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Get Chat", openWorldHint=True, readOnlyHint=True))
@validate_id("chat_id")
async def get_chat(chat_id: ChatRef, include_photos: bool = False) -> str:
    """Details of a group, channel, user or bot: flags, opacity, bio, status, common chats, bot commands, unread, last message.

    Args:
        chat_id: Chat/user ID or @username.
        include_photos: Users only: also list profile photo IDs with dates.
    """
    try:
        entity = await resolve_entity(chat_id)
        result = [f"ID: {entity.id}"]

        if hasattr(entity, "title"):
            result.append(f"Title: {entity.title}")
            result.append(f"Type: {get_entity_type(entity)}")
            if getattr(entity, "username", None):
                result.append(f"Username: @{entity.username}")
            try:
                result.append(f"Participants: {(await client.get_participants(entity, limit=0)).total}")
            except Exception as pe:
                result.append(f"Participants: Error fetching ({pe})")
            flags = [f for f in ("scam", "fake", "restricted", "verified") if getattr(entity, f, False)]
            if flags:
                result.append(f"Flags: {', '.join(flags)}")

            # 结构不透明度：以下每一项都是卖家主动关闭一条核查路径。
            # 「卖家让你查不了」这个行为本身，比任何官方标记都更能说明问题 ——
            # 而在此之前，一个三重不透明的卖货群和一个全开放群在输出里毫无差别。
            opacity = []
            if getattr(entity, "noforwards", False):
                opacity.append("禁止转发/截图外传（你无法把证据转给曝光频道）")
            full = await _try_request(_full_request(entity), chat_id)
            if full:
                fc = full.full_chat
                if getattr(fc, "hidden_prehistory", False):
                    opacity.append("新成员看不到入群前历史（旧差评对新买家不可见）")
                if getattr(fc, "participants_hidden", False):
                    opacity.append("成员列表已隐藏")
                elif not getattr(fc, "can_view_participants", True):
                    opacity.append("不可查看成员（无法核对成员真实性）")
                if getattr(entity, "broadcast", False) and not getattr(fc, "linked_chat_id", None):
                    opacity.append("没有评论区 —— 不是「零差评」，是结构上不可能有公开差评")
            if opacity:
                result.append(f"Opacity ({len(opacity)}): " + "; ".join(opacity))

        elif isinstance(entity, User):
            result.append("Name: " + " ".join(p for p in (entity.first_name, entity.last_name) if p))
            result.append(f"Type: {'Bot' if entity.bot else 'User'}")
            if entity.username:
                result.append(f"Username: @{entity.username}")
            if entity.phone:
                result.append(f"Phone: {entity.phone}")
            # 实体缓存没有过期时间，scam/fake 可能是旧值；GetFullUser 顺带回来的 User 是新的
            full = await _try_request(functions.users.GetFullUserRequest(id=entity), chat_id)
            fresh = next((u for u in full.users if u.id == entity.id), entity) if full else entity
            fu = full.full_user if full else None
            if fu:
                if fu.about:
                    result.append(f"Bio: {fu.about}")
                result.append(f"Common chats: {fu.common_chats_count}")
                bot = fu.bot_info
                if bot and bot.description:
                    result.append(f"Bot description: {bot.description}")
                if bot and bot.commands:
                    result.append("Bot commands: " + "; ".join(f"/{c.command} - {c.description}" for c in bot.commands))
            if getattr(fresh, "bot_active_users", None):
                result.append(f"Bot monthly users: {fresh.bot_active_users}")
            if not entity.bot:
                result.append(f"Status: {_status_text(getattr(fresh, 'status', None))}")
            flags = [f for f in ("scam", "fake", "verified", "premium", "restricted") if getattr(fresh, f, False)]
            if flags:
                result.append(f"Flags: {', '.join(flags)}")
            # 共同群：数量上面已有，这里列出前 20 个，顺藤摸瓜看这人混哪些圈子
            if fu and fu.common_chats_count:
                common = await _try_request(
                    functions.messages.GetCommonChatsRequest(user_id=entity, max_id=0, limit=20), chat_id
                )
                if common and common.chats:
                    result.append("Common chat list: " + "; ".join(f"{c.id} {c.title}" for c in common.chats))
            if include_photos:
                # 头像日期能粗看账号多老：最早一张头像是上周的「老店」值得怀疑
                photos = await _try_request(
                    functions.photos.GetUserPhotosRequest(user_id=entity, offset=0, max_id=0, limit=10), chat_id
                )
                if photos:
                    items = [f"{p.id} ({p.date:%Y-%m-%d})" if getattr(p, "date", None) else str(p.id) for p in photos.photos]
                    result.append("Photos: " + ("; ".join(items) or "none"))

        # 未读数只对自己会话列表里的聊天存在。单点查询，而不是拉回全部会话再过滤 ——
        # get_chat 是调研里最高频的动词。offset_peer 是翻页游标不是过滤器，用它会拿到别的聊天。
        # 直接传 entity，由 Telethon 自己转成入参；不走 resolve_input_entity（测试替身管不到它，会去连真服务器）。
        res = await _try_request(functions.messages.GetPeerDialogsRequest(peers=[entity]), chat_id)
        if res and getattr(res, "dialogs", None):
            result.append(f"Unread Messages: {res.dialogs[0].unread_count}")

        # 最新一条直接从目标本身取，没加入的公开频道也取得到
        try:
            messages = await client.get_messages(entity, limit=1)
            if messages:
                last = messages[0]
                result.append(f"Last Message: From {get_sender_name(last)} at {last.date}")
                result.append(f"Message: {last.message or '[Media/No text]'}")
        except Exception as me:
            logger.warning(f"Could not get latest message for {chat_id}: {me}")

        return "\n".join(result)
    except Exception as e:
        return log_and_format_error("get_chat", e, chat_id=chat_id)


# 传类即可，Telethon 自己实例化（Kicked / Banned 要带空的 q，它会补）
_ROLE_FILTERS = {
    "admin": types.ChannelParticipantsAdmins,
    "bot": types.ChannelParticipantsBots,
    "banned": types.ChannelParticipantsKicked,  # 被踢且不能再进（manage_member ban 的结果）
    "restricted": types.ChannelParticipantsBanned,  # 还在群里但被限制发言
    "recent": types.ChannelParticipantsRecent,
    "all": None,
}


def _role_of(p) -> str:
    part = getattr(p, "participant", None)
    if isinstance(part, types.ChannelParticipantCreator):
        return "creator"
    if isinstance(part, types.ChannelParticipantAdmin):
        return "admin"
    if isinstance(part, types.ChannelParticipantBanned):
        return "banned" if getattr(part.banned_rights, "view_messages", False) else "restricted"
    return "member"


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Get Participants", openWorldHint=True, readOnlyHint=True))
@validate_id("chat_id")
async def get_participants(
    chat_id: ChatRef, limit: int = 100, search: Optional[str] = None, role: Optional[str] = None
) -> str:
    """List members of a group/channel with role, username and join date.

    Args:
        chat_id: Group/channel ID or @username.
        limit: Max members (big groups have tens of thousands; keep it small).
        search: Only members whose name/username matches.
        role: One of: admin, bot, banned, restricted, recent, all. admin tells whether a seller runs the group.
    """
    try:
        if role and role not in _ROLE_FILTERS:
            raise _unknown("role", role, _ROLE_FILTERS)
        entity = await resolve_entity(chat_id)
        kwargs = {"limit": limit}
        if search:
            kwargs["search"] = search
        if _ROLE_FILTERS.get(role):
            kwargs["filter"] = _ROLE_FILTERS[role]
        out = []
        for p in await client.get_participants(entity, **kwargs):
            row = {
                "id": p.id,
                "name": " ".join(x for x in (getattr(p, "first_name", ""), getattr(p, "last_name", "")) if x),
                # username 是把群成员和消息里的 @卖家 对上号的唯一钥匙
                "username": getattr(p, "username", None),
                "role": _role_of(p),
            }
            for flag in ("bot", "premium", "scam", "fake"):
                if getattr(p, flag, False):
                    row[flag] = True
            joined = getattr(getattr(p, "participant", None), "date", None)
            if joined:
                row["joined"] = joined.strftime("%Y-%m-%d")
            out.append(row)
        return dump_json({"count": len(out), "limit": limit, "participants": out})
    except Exception as e:
        return log_and_format_error("get_participants", e, chat_id=chat_id)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="List Topics", openWorldHint=True, readOnlyHint=True))
@validate_id("chat_id")
async def list_topics(chat_id: ChatRef, limit: int = 50, offset_topic: int = 0, search_query: Optional[str] = None) -> str:
    """List forum topics of a supergroup; post into one with send_message(reply_to=topic_id).

    Args:
        chat_id: Forum supergroup ID or @username.
        limit: Max topics.
        offset_topic: Topic ID to page from.
        search_query: Filter topics by title.
    """
    try:
        entity = await resolve_entity(chat_id)
        if not getattr(entity, "forum", False):
            return "This chat is not a supergroup with forum topics enabled."
        # 本机 Telethon 1.42 里这个接口在 messages 下、参数叫 peer；原版写的 channels.GetForumTopicsRequest 已不存在
        result = await client(functions.messages.GetForumTopicsRequest(
            peer=entity, offset_date=None, offset_id=0, offset_topic=offset_topic, limit=limit, q=search_query or None
        ))
        last = {m.id: m.date for m in getattr(result, "messages", None) or []}
        topics = []
        for t in getattr(result, "topics", None) or []:
            row = {"id": t.id, "title": getattr(t, "title", None) or "(no title)"}
            for attr in ("unread_count", "closed", "hidden", "pinned"):
                if getattr(t, attr, None):
                    row[attr] = getattr(t, attr)
            if getattr(t, "top_message", None) in last:
                row["last_activity"] = last[t.top_message]
            topics.append(row)
        return dump_json(topics) if topics else "No topics found for this chat."
    except Exception as e:
        return log_and_format_error("list_topics", e, chat_id=chat_id, offset_topic=offset_topic)


_BAN_FLAGS = ("view_messages", "send_messages", "send_media", "send_stickers", "send_gifs", "send_games",
              "send_inline", "embed_links", "send_polls", "change_info", "invite_users", "pin_messages")
# 不给 rights 时的默认管理员权限（照搬原版）：能管内容和人，但不能再加管理员、不匿名
_DEFAULT_ADMIN_RIGHTS = dict(change_info=True, post_messages=True, edit_messages=True, delete_messages=True,
                             ban_users=True, invite_users=True, pin_messages=True, add_admins=False,
                             anonymous=False, manage_call=True, other=True)
_MEMBER_ACTIONS = ("invite", "ban", "unban", "promote", "demote")
_MEMBER_ERRORS = {
    "UserNotMutualContactError": "Not a mutual contact; they must add you back first.",
    "UserPrivacyRestrictedError": "Their privacy settings prevent adding them.",
    "UserAlreadyParticipantError": "Already a member.",
}


def _member_request(action: str, chat, user, rights: Optional[Dict[str, bool]]):
    if isinstance(chat, Chat):
        # 普通群（非超级群）走 messages.* 接口；原版一律用 channels.*，对普通群直接报类型错
        if action == "invite":
            return functions.messages.AddChatUserRequest(chat_id=chat.id, user_id=user, fwd_limit=100)
        if action == "ban":
            return functions.messages.DeleteChatUserRequest(chat_id=chat.id, user_id=user)
        if action in ("promote", "demote"):
            return functions.messages.EditChatAdminRequest(chat_id=chat.id, user_id=user, is_admin=action == "promote")
        raise ValidationError("Basic groups keep no ban list; just invite the user again.")
    if action == "invite":
        return functions.channels.InviteToChannelRequest(channel=chat, users=[user])
    if action in ("ban", "unban"):
        banned = types.ChatBannedRights(until_date=None, **{f: action == "ban" for f in _BAN_FLAGS})
        return functions.channels.EditBannedRequest(channel=chat, participant=user, banned_rights=banned)
    if action == "promote":
        admin = types.ChatAdminRights(**{**_DEFAULT_ADMIN_RIGHTS, **(rights or {})})
        return functions.channels.EditAdminRequest(channel=chat, user_id=user, admin_rights=admin, rank="Admin")
    return functions.channels.EditAdminRequest(channel=chat, user_id=user, admin_rights=types.ChatAdminRights(), rank="")


async def _apply_member_action(action: str, chat, users: list, rights=None) -> List[dict]:
    """逐人执行：一个人因隐私设置拉不进来，不该让其余人一起失败。"""
    rows = []
    for user in users:
        row = {"user_id": user.id}
        try:
            res = await client(_member_request(action, chat, user, rights))
            # 新版接口拉人时，被隐私拦住的人不报错，只出现在 missing_invitees 里
            if getattr(res, "missing_invitees", None):
                row["error"] = _MEMBER_ERRORS["UserPrivacyRestrictedError"]
        except Exception as e:
            logger.warning(f"{action} {user.id} in {chat.id} failed: {e}")
            row["error"] = _MEMBER_ERRORS.get(type(e).__name__, str(e))
        rows.append(row)
    return rows


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Create Chat", openWorldHint=True, readOnlyHint=False, destructiveHint=False))
@validate_id("user_ids")
async def create_chat(title: str, kind: str = "group", about: str = "", user_ids: Optional[List[ChatRef]] = None) -> str:
    """Create a basic group, supergroup or channel and add user_ids right away.

    Args:
        title: Chat title.
        kind: One of: group, supergroup, channel. A basic group needs user_ids.
        about: Description (supergroup/channel only).
        user_ids: Users to add (IDs or @usernames).
    """
    try:
        if kind not in ("group", "supergroup", "channel"):
            raise _unknown("kind", kind, ("group", "supergroup", "channel"))
        users = [await resolve_entity(u) for u in user_ids or []]
        if kind == "group":
            if not users:
                raise ValidationError("A basic group needs at least one user in user_ids.")
            result = await client(functions.messages.CreateChatRequest(users=users, title=title))
            # 新版接口返回 InvitedUsers：群在 updates.chats 里，被隐私拦住的人在 missing_invitees
            chat = getattr(result, "updates", result).chats[0]
            missing = [m.user_id for m in getattr(result, "missing_invitees", None) or []]
        else:
            result = await client(functions.channels.CreateChannelRequest(
                title=title, about=about, megagroup=kind == "supergroup", broadcast=kind == "channel"
            ))
            chat = result.chats[0]
            missing = [r["user_id"] for r in await _apply_member_action("invite", chat, users) if "error" in r]
        msg = f"Created {kind} '{title}' (ID: {chat.id})."
        return msg + (f" Could not add: {missing}." if missing else "")
    except Exception as e:
        return log_and_format_error("create_chat", e, title=title, kind=kind)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Edit Chat", openWorldHint=True, readOnlyHint=False, destructiveHint=True))
@validate_id("chat_id")
async def edit_chat(
    chat_id: ChatRef,
    title: Optional[str] = None,
    photo_path: Optional[str] = None,
    delete_photo: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Change a group/channel's title and/or photo, or delete its photo.

    Args:
        chat_id: Group/channel ID or @username.
        title: New title.
        photo_path: Local image (jpg/png/webp) for the new photo.
        delete_photo: Remove the current photo.
    """
    try:
        if not (title or photo_path or delete_photo):
            raise ValidationError("Give at least one of: title, photo_path, delete_photo.")
        if photo_path:
            path, err = await readable_path(photo_path, ctx, kind="photo")
            if err:  # 路径白名单不许绕；用 ValidationError 抛，拒绝原因才能原样到调用方
                raise ValidationError(err)
        entity = await resolve_entity(chat_id)
        if not isinstance(entity, (Chat, Channel)):
            raise ValidationError(f"{chat_id} is a {get_entity_type(entity)}; only groups/channels can be edited.")
        basic = isinstance(entity, Chat)
        done = []
        if title:
            await client(functions.messages.EditChatTitleRequest(chat_id=entity.id, title=title) if basic
                         else functions.channels.EditTitleRequest(channel=entity, title=title))
            done.append(f"title set to '{title}'")
        if photo_path or delete_photo:
            if photo_path:
                photo = types.InputChatUploadedPhoto(file=await client.upload_file(str(path)))
            else:
                photo = types.InputChatPhotoEmpty()
            await client(functions.messages.EditChatPhotoRequest(chat_id=entity.id, photo=photo) if basic
                         else functions.channels.EditPhotoRequest(channel=entity, photo=photo))
            done.append("photo updated" if photo_path else "photo deleted")
        return f"Chat {entity.id}: {', '.join(done)}."
    except Exception as e:
        return log_and_format_error("edit_chat", e, chat_id=chat_id)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Manage Member", openWorldHint=True, readOnlyHint=False, destructiveHint=True))
@validate_id("chat_id", "user_ids")
async def manage_member(
    chat_id: ChatRef, user_ids: Union[ChatRef, List[ChatRef]], action: str, rights: Optional[Dict[str, bool]] = None
) -> str:
    """Invite, ban, unban, promote or demote one or more users in a group/channel.

    Args:
        chat_id: Group/channel ID or @username.
        user_ids: One user or a list (IDs or @usernames).
        action: One of: invite, ban, unban, promote, demote.
        rights: promote only: admin rights to override, e.g. {"add_admins": true}.
    """
    try:
        if action not in _MEMBER_ACTIONS:
            raise _unknown("action", action, _MEMBER_ACTIONS)
        chat = await resolve_entity(chat_id)
        users = [await resolve_entity(u) for u in (user_ids if isinstance(user_ids, list) else [user_ids])]
        rows = await _apply_member_action(action, chat, users, rights)
    except Exception as e:
        return log_and_format_error("manage_member", e, chat_id=chat_id, action=action)
    if all("error" in r for r in rows):  # 一个都没成才算出错；部分失败看 results 里的 error
        msg = f"{action} failed: " + "; ".join(f"{r['user_id']}: {r['error']}" for r in rows)
        return log_and_format_error("manage_member", RuntimeError(msg), user_message=msg, chat_id=chat_id, action=action)
    return dump_json({"action": action, "chat_id": chat.id, "results": rows})


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Get Invite Link", openWorldHint=True, readOnlyHint=False, destructiveHint=False))
@validate_id("chat_id")
async def get_invite_link(chat_id: ChatRef, new: bool = False) -> str:
    """Get a group/channel's invite link; new=True creates an extra one (admin only).

    Args:
        chat_id: Group/channel ID or @username.
        new: Create a new invite link instead of reading the existing one.
    """
    try:
        entity = await resolve_entity(chat_id)
        # 原版「读链接」第一步就调 ExportChatInvite，每读一次就多造一条链接；这里读现成的，new=True 才新建
        if new:
            return (await client(functions.messages.ExportChatInviteRequest(peer=entity))).link
        invite = getattr((await client(_full_request(entity))).full_chat, "exported_invite", None)
        if invite:
            return invite.link
        if getattr(entity, "username", None):
            return f"https://t.me/{entity.username}"
        return "No invite link yet (creating one needs admin rights). Call again with new=True."
    except Exception as e:
        return log_and_format_error("get_invite_link", e, chat_id=chat_id, new=new)


_LINK_RE = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/([^?#]+)", re.I)
_JOIN_ERRORS = {
    "InviteHashExpiredError": "The invite link has expired.",
    "InviteHashInvalidError": "The invite link is invalid.",
    "UsersTooMuchError": "The chat is full.",
    "ChannelsTooMuchError": "You have joined too many chats.",
    "ChannelPrivateError": "This chat is private; an invite link is required.",
    "ValueError": "Cannot find that chat: check the @username, ID or invite link.",
}


async def _join_by_hash(invite_hash: str) -> str:
    invite = await client(functions.messages.CheckChatInviteRequest(hash=invite_hash))
    # ChatInviteAlready 才是已经在里面；ChatInvitePeek 也带 chat 但只是能预览（原版把两者混了）
    if isinstance(invite, types.ChatInviteAlready):
        return f"Already a member of {invite.chat.title} (ID: {invite.chat.id})."
    result = await client(functions.messages.ImportChatInviteRequest(hash=invite_hash))
    chats = getattr(result, "chats", None)
    return f"Joined {chats[0].title} (ID: {chats[0].id})." if chats else "Joined chat via invite link."


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Join Chat", openWorldHint=True, readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def join_chat(target: ChatRef) -> str:
    """Join a public channel/group or a private chat via invite link.

    Args:
        target: @username, chat ID, t.me/name, t.me/+hash, t.me/joinchat/hash, or a bare invite hash.
    """
    try:
        value = target.strip() if isinstance(target, str) else target
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
            value = int(value)
        invite_hash = None
        m = _LINK_RE.match(value) if isinstance(value, str) else None
        if m:
            path = m.group(1).strip("/")
            if path.startswith("+"):
                invite_hash = path[1:]
            elif path.lower().startswith("joinchat/"):
                invite_hash = path.split("/", 1)[1]
            else:
                value = path.split("/")[0]  # t.me/name/123 也是公开频道
        elif isinstance(value, str) and value.startswith("+"):
            invite_hash = value[1:]
        elif isinstance(value, str) and not value.startswith("@"):
            # 裸字符串分不清是邀请码还是用户名：先当邀请码查一次（便宜、不触发用户名限速），不是再当用户名
            # 用户名被当邀请码查时，服务器既可能回「无效」也可能回「过期」，两种都要接着当用户名试
            try:
                return await _join_by_hash(value)
            except (errors.InviteHashInvalidError, errors.InviteHashExpiredError):
                pass
        if invite_hash:
            return await _join_by_hash(invite_hash)
        entity = await resolve_entity(value)
        try:
            await client(functions.channels.JoinChannelRequest(channel=entity))
        except errors.UserAlreadyParticipantError:
            return f"Already a member of {getattr(entity, 'title', value)}."
        return f"Joined {getattr(entity, 'title', value)} (ID: {entity.id})."
    except errors.InviteRequestSentError:
        return "Join request sent; waiting for admin approval."
    except errors.UserAlreadyParticipantError:
        return "Already a member of this chat."
    except Exception as e:
        return log_and_format_error("join_chat", e, user_message=_JOIN_ERRORS.get(type(e).__name__), target=target)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Leave Chat", openWorldHint=True, readOnlyHint=False, destructiveHint=True, idempotentHint=True))
@validate_id("chat_id")
async def leave_chat(chat_id: ChatRef) -> str:
    """Leave a group or channel.

    Args:
        chat_id: Group/channel ID or @username.
    """
    try:
        entity = await resolve_entity(chat_id)
        if isinstance(entity, Channel):  # 超级群也是 Channel
            await client(functions.channels.LeaveChannelRequest(channel=entity))
        elif isinstance(entity, Chat):
            # 普通群没有「退出」接口，是把自己从成员里删掉。现版先 get_me 再退、失败换 me.id 重试；
            # InputUserSelf 就是「我自己」，一步到位
            await client(functions.messages.DeleteChatUserRequest(chat_id=entity.id, user_id=types.InputUserSelf()))
        else:
            raise ValidationError(f"{chat_id} is a {get_entity_type(entity)}; only groups/channels can be left.")
        return f"Left {entity.title} (ID: {entity.id})."
    except Exception as e:
        return log_and_format_error("leave_chat", e, chat_id=chat_id)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Get Recent Actions", openWorldHint=True, readOnlyHint=True))
@validate_id("chat_id")
async def get_recent_actions(chat_id: ChatRef, limit: int = 50) -> str:
    """Admin log of a supergroup/channel: who deleted, edited, banned or renamed what (admin only).

    Args:
        chat_id: Supergroup/channel ID or @username.
        limit: Max events.
    """
    try:
        entity = await resolve_entity(chat_id)
        result = await client(functions.channels.GetAdminLogRequest(channel=entity, q="", max_id=0, min_id=0, limit=limit))
        # 原版把每个事件 to_dict 整个倒出来，一条几 KB；这里只留谁、何时、干了啥、涉及的文字
        names = {u.id: format_entity(u).get("name") for u in getattr(result, "users", None) or []}
        events = []
        for ev in result.events:
            row = {"id": ev.id, "date": ev.date, "by": names.get(ev.user_id, ev.user_id),
                   "action": type(ev.action).__name__.replace("ChannelAdminLogEventAction", "")}
            for attr in ("message", "new_message"):  # 被删 / 被改 / 被置顶的那条消息
                text = getattr(getattr(ev.action, attr, None), "message", None)
                if text:
                    row["text"] = text[:200]
            for attr in ("prev_value", "new_value"):  # 改名、改简介、改用户名
                v = getattr(ev.action, attr, None)
                if isinstance(v, (str, int, bool)):
                    row[attr] = v
            events.append(row)
        return dump_json(events) if events else "No recent admin actions found."
    except Exception as e:
        return log_and_format_error("get_recent_actions", e, chat_id=chat_id)


_CHAT_STATES = ("mute", "unmute", "archive", "unarchive")


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Set Chat State", openWorldHint=True, readOnlyHint=False, destructiveHint=False, idempotentHint=True))
@validate_id("chat_id")
async def set_chat_state(chat_id: ChatRef, action: str) -> str:
    """Mute, unmute, archive or unarchive a chat.

    Args:
        chat_id: Chat ID or @username.
        action: One of: mute, unmute, archive, unarchive.
    """
    try:
        if action not in _CHAT_STATES:
            raise _unknown("action", action, _CHAT_STATES)
        entity = await resolve_entity(chat_id)
        if action in ("mute", "unmute"):
            # 2**31-1 = 永久静音（Telegram 的约定），0 = 取消静音
            settings = types.InputPeerNotifySettings(mute_until=2**31 - 1 if action == "mute" else 0)
            await client(functions.account.UpdateNotifySettingsRequest(peer=entity, settings=settings))
        else:
            # 归档 = 移到 1 号文件夹，取消 = 移回 0 号；这里嵌套在 InputFolderPeer 里，Telethon 不会自动转换，要手动 get_input_peer
            folder = types.InputFolderPeer(peer=utils.get_input_peer(entity), folder_id=1 if action == "archive" else 0)
            await client(functions.folders.EditPeerFoldersRequest(folder_peers=[folder]))
        return f"Chat {chat_id}: {action}d."
    except Exception as e:
        return log_and_format_error("set_chat_state", e, chat_id=chat_id, action=action)
