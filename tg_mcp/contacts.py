"""通讯录：查询 / 搜索 / 屏蔽名单 / 导出，以及添加、删除、屏蔽、解除屏蔽、批量导入。"""

from typing import Dict, List, Optional

from mcp.types import ToolAnnotations
from telethon import functions, types

from .app import client, mcp
from .common import ChatRef, ValidationError, dump_json, log_and_format_error, require, resolve_entity, validate_id


def _row(u) -> dict:
    """一个人一行：编号（直接能当 chat_id 去聊）、名字、用户名、手机号；空的不输出。"""
    row = {"id": u.id, "name": " ".join(p for p in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if p)}
    for k in ("username", "phone"):
        if getattr(u, k, None):
            row[k] = getattr(u, k)
    return row


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="List Contacts", openWorldHint=True, readOnlyHint=True))
async def list_contacts(query: Optional[str] = None, blocked_only: bool = False) -> str:
    """List your contacts as JSON (id, name, username, phone); a query searches by name, username or phone.

    Args:
        query: Search term; hits also include non-contacts (marked "contact": true when saved).
        blocked_only: List blocked users instead (first 100; ignores query).
    """
    try:
        if blocked_only:
            res = await client(functions.contacts.GetBlockedRequest(offset=0, limit=100))
            # count 是总数；比 blocked 条数大说明被截断了
            return dump_json({"count": getattr(res, "count", len(res.users)), "blocked": [_row(u) for u in res.users]})
        if query:
            res = await client(functions.contacts.SearchRequest(q=query, limit=50))
            # 搜索结果里混着陌生人，标出哪些真的存了通讯录
            rows = [{**_row(u), **({"contact": True} if getattr(u, "contact", False) else {})} for u in res.users]
        else:
            res = await client(functions.contacts.GetContactsRequest(hash=0))
            rows = [_row(u) for u in res.users]
        return dump_json({"count": len(rows), "contacts": rows})
    except Exception as e:
        return log_and_format_error("list_contacts", e, query=query, blocked_only=blocked_only)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Manage Contact", openWorldHint=True, readOnlyHint=False, destructiveHint=True))
@validate_id("user_id")
async def manage_contact(
    action: str,
    user_id: Optional[ChatRef] = None,
    phone: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    username: Optional[str] = None,
    contacts: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Add, delete, block or unblock a contact, or bulk-import contacts by phone.

    Args:
        action: One of: add, delete, block, unblock, import.
        user_id: User ID or @username (delete/block/unblock; also lets add use an already-known user).
        phone: Phone with country code (add by phone; first_name then required).
        first_name: Name to save the contact under. last_name: optional.
        username: Username to add without needing a phone number (add).
        contacts: List of {phone, first_name, last_name} dicts (import).
    """
    try:
        if action == "add":
            if phone:
                require(action, first_name=first_name)
                res = await client(functions.contacts.ImportContactsRequest(contacts=[
                    types.InputPhoneContact(client_id=0, phone=phone, first_name=first_name, last_name=last_name or "")
                ]))
                if res.imported:
                    return f"Contact {first_name} ({phone}) added."
                return f"Telegram did not import {phone}: the number may not be on Telegram, or its owner hides it."
            if username is None and user_id is None:
                raise ValidationError("Action 'add' requires phone, username or user_id.")
            user = await resolve_entity(username.lstrip("@") if username else user_id)
            if not isinstance(user, types.User):
                raise ValidationError("That is not a user (it looks like a group or channel).")
            # 按用户名 / 编号添加不需要手机号，phone 传空串（原版就是这么做的）
            name = first_name or user.first_name or ""
            await client(functions.contacts.AddContactRequest(id=user, first_name=name, last_name=last_name or "", phone=""))
            return f"Contact {name} (id {user.id}) added."

        if action in ("delete", "block", "unblock"):
            require(action, user_id=user_id)
            user = await resolve_entity(user_id)
            if action == "delete":
                await client(functions.contacts.DeleteContactsRequest(id=[user]))
                return f"Contact {user_id} deleted."
            request = functions.contacts.BlockRequest if action == "block" else functions.contacts.UnblockRequest
            await client(request(id=user))
            return f"User {user_id} {action}ed."

        if action == "import":
            require(action, contacts=contacts)
            for i, c in enumerate(contacts):
                if not c.get("phone") or not c.get("first_name"):
                    raise ValidationError(f"contacts[{i}] needs phone and first_name.")
            # 注意是 types.InputPhoneContact；原版写成 functions.contacts.InputPhoneContact，库里根本没有这个名字
            res = await client(functions.contacts.ImportContactsRequest(contacts=[
                types.InputPhoneContact(client_id=i, phone=c["phone"], first_name=c["first_name"], last_name=c.get("last_name", ""))
                for i, c in enumerate(contacts)
            ]))
            return dump_json({"submitted": len(contacts), "imported": len(res.imported)})

        raise ValidationError(f"Unknown action: {action}. Use one of: add, delete, block, unblock, import.")
    except Exception as e:
        return log_and_format_error("manage_contact", e, action=action, user_id=user_id)
