"""聊天分组（Telegram 里叫 dialog filter）：列出 / 看详情、创建、删除、增删聊天、排序。"""

from typing import List, Optional

from mcp.types import ToolAnnotations
from telethon import functions, types, utils

from .app import client, mcp
from .common import (
    ChatRef,
    ValidationError,
    dump_json,
    get_entity_type,
    log_and_format_error,
    require,
    resolve_entity,
    resolve_input_entity,
    validate_id,
)

# 条件型分组的 8 个开关（共享分组 DialogFilterChatlist 没有这些）
_FLAGS = ("contacts", "non_contacts", "groups", "broadcasts", "bots", "exclude_muted", "exclude_read", "exclude_archived")


def _title(f) -> str:
    # 新版 Telegram 的分组标题是 TextWithEntities，老数据是 str
    return f.title.text if isinstance(f.title, types.TextWithEntities) else f.title


async def _get_folders() -> list:
    """全部自定义分组（含共享分组）；系统默认的「全部聊天」不算。"""
    result = await client(functions.messages.GetDialogFiltersRequest())
    return [f for f in result.filters if isinstance(f, (types.DialogFilter, types.DialogFilterChatlist))]


_CATEGORY_FLAGS = ("contacts", "non_contacts", "groups", "broadcasts", "bots")


def _find(folders: list, folder_id: int):
    for f in folders:
        if f.id == folder_id:
            return f
    raise ValidationError(f"Folder {folder_id} not found. Use list_folders to see available folders.")


async def _input_peers(chat_ids: List[ChatRef]) -> list:
    peers = []
    for c in chat_ids:
        try:
            peer = await resolve_input_entity(c)
            if isinstance(peer, types.InputPeerSelf):
                # "me" 解析出来的 InputPeerSelf 没有编号，后面按编号去重会炸。
                # get_input_peer 对自己还是给 InputPeerSelf，所以直接按编号构造
                me = await resolve_entity("me")
                peer = types.InputPeerUser(me.id, me.access_hash or 0)
            peers.append(peer)
        except Exception as e:
            raise ValidationError(f"Failed to resolve chat '{c}': {e}") from e
    return peers


def _summary(f) -> dict:
    """列表里每个分组一行：编号、标题、各类聊天数；开关只列出打开的那几个。"""
    row = {"id": f.id, "title": _title(f)}
    if f.emoticon:
        row["emoticon"] = f.emoticon
    row["chats"], row["pinned"] = len(f.include_peers), len(f.pinned_peers)
    if isinstance(f, types.DialogFilterChatlist):
        row["shared"] = True
    else:
        row["excluded"] = len(f.exclude_peers)
        row["flags"] = [k for k in _FLAGS if getattr(f, k, False)]
    return row


async def _peer_row(peer) -> dict:
    # 分组里存的是 InputPeer，这种对象不能哈希，不能直接丢给 resolve_entity（它拿参数当缓存键，
    # 原版因此每个聊天都落进 except、全显示 Unknown）。先转成带 -100 前缀的编号再解析，
    # 这个编号也正是别的工具的 chat_id 要的形式。
    pid = None
    try:
        pid = utils.get_peer_id(peer)
        e = await resolve_entity(pid)
    except Exception:
        return {"id": pid, "name": "Unknown"}
    row = {"id": pid, "name": getattr(e, "title", None) or getattr(e, "first_name", None) or "Unknown", "type": get_entity_type(e)}
    if getattr(e, "username", None):
        row["username"] = e.username
    return row


def _rebuilt(f, include_peers: list, pinned_peers: list):
    """改完聊天名单后重建分组对象。Telegram 的更新是整体覆盖，漏抄任何字段都会把原设置清掉，所以其余字段原样带过去。"""
    keep = dict(id=f.id, title=f.title, emoticon=f.emoticon, title_noanimate=f.title_noanimate, color=f.color,
                pinned_peers=pinned_peers, include_peers=include_peers)
    if isinstance(f, types.DialogFilterChatlist):
        return types.DialogFilterChatlist(has_my_invites=f.has_my_invites, **keep)
    return types.DialogFilter(exclude_peers=list(f.exclude_peers), **{k: getattr(f, k) for k in _FLAGS}, **keep)


async def _save(folder_id: int, new_filter) -> None:
    """写回分组；new_filter=None 即删除。"""
    try:
        await client(functions.messages.UpdateDialogFilterRequest(id=folder_id, filter=new_filter))
    except Exception as e:
        # 上限随账号不同（普通 10 个、Premium 20 个），不在本地写死，由服务器说了算
        if "DIALOG_FILTERS_TOO_MUCH" in str(e):
            raise ValidationError("Telegram folder limit reached (10, or 20 with Premium). Delete one first.") from e
        raise


async def _dropped(folder_id: int, peers: list) -> list:
    """Telegram 会悄悄丢掉你没加入的聊天，写入也不报错，所以存完读回来比对，返回没进去的编号。"""
    saved = _find(await _get_folders(), folder_id)
    have = {utils.get_peer_id(p) for p in saved.include_peers + saved.pinned_peers}
    return [pid for pid in map(utils.get_peer_id, peers) if pid not in have]


_DROPPED_HINT = "Telegram drops chats you have not joined; join_chat them first, then add_chat."


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="List Folders", openWorldHint=True, readOnlyHint=True))
async def list_folders(folder_id: Optional[int] = None) -> str:
    """List chat folders; with folder_id, show that folder in detail including every chat in it.

    Args:
        folder_id: Folder ID to inspect. Omit to list all folders.
    """
    try:
        folders = await _get_folders()
        if folder_id is not None:
            f = _find(folders, folder_id)
            detail = _summary(f)
            detail["chats"] = [await _peer_row(p) for p in f.include_peers]
            detail["pinned"] = [await _peer_row(p) for p in f.pinned_peers]
            if not isinstance(f, types.DialogFilterChatlist):
                detail["excluded"] = [await _peer_row(p) for p in f.exclude_peers]
            return dump_json(detail)
        return dump_json({"count": len(folders), "folders": [_summary(f) for f in folders]})
    except Exception as e:
        return log_and_format_error("list_folders", e, folder_id=folder_id)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Manage Folder", openWorldHint=True, readOnlyHint=False, destructiveHint=True))
@validate_id("chat_ids")
async def manage_folder(
    action: str,
    folder_id: Optional[int] = None,
    title: Optional[str] = None,
    emoticon: Optional[str] = None,
    chat_ids: Optional[List[ChatRef]] = None,
    pinned: bool = False,
    folder_ids: Optional[List[int]] = None,
    contacts: bool = False,
    non_contacts: bool = False,
    groups: bool = False,
    broadcasts: bool = False,
    bots: bool = False,
    exclude_muted: bool = False,
    exclude_read: bool = False,
    exclude_archived: bool = False,
) -> str:
    """Create, delete, reorder folders or add/remove chats in one.

    Args:
        action: One of: create, delete, add_chat, remove_chat, reorder.
        folder_id: Target folder (from list_folders); for delete, add_chat, remove_chat.
        title: Folder name (create). emoticon: optional emoji, e.g. a briefcase.
        chat_ids: Chats to put in (create), add or remove. pinned: also pin them.
        folder_ids: ALL folder IDs in the desired order (reorder).
        contacts, non_contacts, groups, broadcasts, bots: auto-include that kind of chat (create).
        exclude_muted, exclude_read, exclude_archived: auto-exclude those chats (create).
    """
    try:
        if action == "create":
            require(action, title=title)
            used = {f.id for f in await _get_folders()}
            new_id = 2  # 0、1 是系统保留编号
            while new_id in used:
                new_id += 1
            peers = await _input_peers(chat_ids or [])
            await _save(new_id, types.DialogFilter(
                id=new_id, title=types.TextWithEntities(text=title, entities=[]), emoticon=emoticon,
                pinned_peers=list(peers) if pinned else [], include_peers=peers, exclude_peers=[],
                contacts=contacts, non_contacts=non_contacts, groups=groups, broadcasts=broadcasts, bots=bots,
                exclude_muted=exclude_muted, exclude_read=exclude_read, exclude_archived=exclude_archived,
            ))
            dropped = await _dropped(new_id, peers)
            out = {"folder_id": new_id, "title": title, "chats": len(peers) - len(dropped)}
            if dropped:
                out.update(not_added=dropped, hint=_DROPPED_HINT)
            return dump_json(out)

        if action == "delete":
            require(action, folder_id=folder_id)
            if folder_id < 2:
                raise ValidationError(f"Cannot delete system folder (ID {folder_id}).")
            f = _find(await _get_folders(), folder_id)
            await _save(folder_id, None)  # filter=None 即删除
            return f"Folder '{_title(f)}' (ID {folder_id}) deleted. Its chats are untouched."

        if action in ("add_chat", "remove_chat"):
            require(action, folder_id=folder_id, chat_ids=chat_ids)
            f = _find(await _get_folders(), folder_id)
            peers = await _input_peers(chat_ids)
            include, pins = list(f.include_peers), list(f.pinned_peers)
            if action == "add_chat":
                have = {utils.get_peer_id(p) for p in include + pins}
                pinned_have = {utils.get_peer_id(p) for p in pins}
                for p in peers:
                    pid = utils.get_peer_id(p)
                    if pid not in have:
                        include.append(p)
                        have.add(pid)
                    if pinned and pid not in pinned_have:
                        pins.append(p)
                        pinned_have.add(pid)
                unchanged = (len(include), len(pins)) == (len(f.include_peers), len(f.pinned_peers))
                verb = "added to"
            else:
                gone = {utils.get_peer_id(p) for p in peers}
                include = [p for p in include if utils.get_peer_id(p) not in gone]
                pins = [p for p in pins if utils.get_peer_id(p) not in gone]
                # Telegram 不许分组空着（FilterIncludeEmptyError），提前说清楚而不是让它抛一串错误码
                if not include and not pins and not any(getattr(f, k, False) for k in _CATEGORY_FLAGS):
                    raise ValidationError("A folder must keep at least one chat or one category flag; delete the folder instead.")
                unchanged = (len(include), len(pins)) == (len(f.include_peers), len(f.pinned_peers))
                verb = "removed from"
            if unchanged:  # 幂等：没有任何变化就不发请求
                return f"Nothing to do: folder {folder_id} already {'has' if action == 'add_chat' else 'lacks'} those chats."
            await _save(folder_id, _rebuilt(f, include, pins))
            if action == "add_chat":
                dropped = await _dropped(folder_id, peers)
                if dropped:
                    return f"{len(peers) - len(dropped)} chat(s) added to folder {folder_id}; not added: {dropped}. {_DROPPED_HINT}"
            return f"{len(peers)} chat(s) {verb} folder {folder_id}" + (" (pinned)." if pinned and action == "add_chat" else ".")

        if action == "reorder":
            require(action, folder_ids=folder_ids)
            existing = {f.id for f in await _get_folders()}
            if set(folder_ids) != existing:
                raise ValidationError(
                    f"folder_ids must list every folder exactly once. Unknown: {sorted(set(folder_ids) - existing)}, "
                    f"missing: {sorted(existing - set(folder_ids))}."
                )
            await client(functions.messages.UpdateDialogFiltersOrderRequest(order=folder_ids))
            return f"Folders reordered: {folder_ids}"

        raise ValidationError(f"Unknown action: {action}. Use one of: create, delete, add_chat, remove_chat, reorder.")
    except Exception as e:
        return log_and_format_error("manage_folder", e, action=action, folder_id=folder_id)
