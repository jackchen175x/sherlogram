"""自己的账号：查看自己、改资料 / 头像、隐私设置、机器人命令、贴纸包。"""

from typing import Dict, List, Optional

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from telethon import functions, types, utils

from .app import client, mcp
from .common import (
    ChatRef,
    ValidationError,
    dump_json,
    format_entity,
    log_and_format_error,
    readable_path,
    resolve_entity,
    validate_id,
)

# 简写 -> 隐私项。其余项（群邀请、语音通话等）没人要过，要用再加
_PRIVACY_KEYS = {
    "status": types.InputPrivacyKeyStatusTimestamp,
    "phone": types.InputPrivacyKeyPhoneNumber,
    "profile_photo": types.InputPrivacyKeyProfilePhoto,
}


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Get Me", openWorldHint=True, readOnlyHint=True))
async def get_me() -> str:
    """Get your own account info (id, name, username, phone)."""
    try:
        return dump_json(format_entity(await client.get_me()))
    except Exception as e:
        return log_and_format_error("get_me", e)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Update Profile", openWorldHint=True, readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def update_profile(
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    about: Optional[str] = None,
    photo_path: Optional[str] = None,
    delete_photo: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Update your name, bio or profile photo; give at least one field.

    Args:
        first_name: New first name. last_name: New last name. about: New bio (empty string clears it).
        photo_path: Local image file (jpg/png/webp) to set as profile photo.
        delete_photo: Remove your current profile photo.
    """
    try:
        text_changes = {"first_name": first_name, "last_name": last_name, "about": about}
        done = [k for k, v in text_changes.items() if v is not None]
        if photo_path and delete_photo:
            raise ValidationError("Give either photo_path or delete_photo, not both.")
        if not (done or photo_path or delete_photo):
            raise ValidationError("Nothing to update: give first_name, last_name, about, photo_path or delete_photo.")
        if done:
            # 没传的字段保持 None，Telegram 不会改动它们
            await client(functions.account.UpdateProfileRequest(**text_changes))
        if photo_path:
            path, err = await readable_path(photo_path, ctx, "photo")
            if err:
                raise ValidationError(err)
            await client(functions.photos.UploadProfilePhotoRequest(file=await client.upload_file(str(path))))
            done.append("photo")
        if delete_photo:
            photos = await client(functions.photos.GetUserPhotosRequest(user_id="me", offset=0, max_id=0, limit=1))
            if not photos.photos:
                done.append("photo (none to delete)")
            else:
                await client(functions.photos.DeletePhotosRequest(id=[photos.photos[0]]))
                done.append("photo deleted")
        return f"Profile updated: {', '.join(done)}."
    except Exception as e:
        return log_and_format_error("update_profile", e, photo_path=photo_path)


async def _input_users(users: List[ChatRef]) -> list:
    """用户 -> InputUser。任何一个找不到就整体报错：隐私名单漏掉一个人就等于没挡住他，不能静默跳过（原版是跳过）。"""
    inputs, failed = [], []
    for u in users:
        try:
            # 必须转成 InputUser：规则对象是嵌套的，Telethon 不会替我们转换里面的实体
            inputs.append(utils.get_input_user(await resolve_entity(u)))
        except Exception:
            failed.append(u)
    if failed:
        raise ValidationError(f"Could not resolve users: {failed}. Nothing was changed.")
    return inputs


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Privacy Settings", openWorldHint=True, readOnlyHint=False, destructiveHint=True, idempotentHint=True))
@validate_id("allow_users", "disallow_users")
async def privacy(
    action: str = "get",
    key: Optional[str] = None,
    allow_users: Optional[List[ChatRef]] = None,
    disallow_users: Optional[List[ChatRef]] = None,
) -> str:
    """Read or change who can see your last seen, phone number or profile photo.

    Args:
        action: One of: get, set.
        key: One of: status (last seen), phone, profile_photo. Defaults to status for get; required for set.
        allow_users: set only. Visible to just these users; leave empty to allow everyone.
        disallow_users: set only. Always hidden from these users, even if everyone else is allowed.
    """
    try:
        if action not in ("get", "set"):
            raise ValidationError(f"Unknown action: {action}. Use one of: get, set.")
        if action == "get":
            key = key or "status"
        if key not in _PRIVACY_KEYS:
            raise ValidationError(f"key must be one of: {', '.join(_PRIVACY_KEYS)}.")
        if action == "get":
            res = await client(functions.account.GetPrivacyRequest(key=_PRIVACY_KEYS[key]()))
            rules = []
            for r in res.rules:
                ids = list(getattr(r, "users", None) or []) + list(getattr(r, "chats", None) or [])
                rules.append({"rule": type(r).__name__.replace("PrivacyValue", ""), **({"ids": ids} if ids else {})})
            return dump_json({"key": key, "rules": rules})

        allow = await _input_users(allow_users or [])
        disallow = await _input_users(disallow_users or [])
        rules = [types.InputPrivacyValueAllowUsers(users=allow) if allow else types.InputPrivacyValueAllowAll()]
        if disallow:
            rules.append(types.InputPrivacyValueDisallowUsers(users=disallow))
        await client(functions.account.SetPrivacyRequest(key=_PRIVACY_KEYS[key](), rules=rules))
        who = f"only {len(allow)} user(s)" if allow else "everyone"
        return f"Privacy '{key}' set: visible to {who}" + (f", hidden from {len(disallow)} user(s)." if disallow else ".")
    except Exception as e:
        return log_and_format_error("privacy", e, action=action, key=key)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Set Bot Commands", openWorldHint=True, readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def set_bot_commands(bot_username: str, commands: List[Dict[str, str]]) -> str:
    """Set the command menu of a bot. Only works when this client is logged in as that bot account.

    Args:
        bot_username: The bot's username; must be the account you are logged in as.
        commands: List of {"command": "start", "description": "Start the bot"} dicts.
    """
    try:
        me = await client.get_me()
        if not getattr(me, "bot", False):
            raise ValidationError("This client is a regular user account, not a bot; Telegram only lets a bot set its own commands.")
        # Telegram 的这个接口永远改的是「当前登录的这个机器人」。原版解析了 bot_username 却没用，
        # 传别的机器人也会回报成功，实际改的是自己，所以这里先核对。
        if bot_username.lstrip("@").lower() != (me.username or "").lower():
            raise ValidationError(f"Logged in as @{me.username}, so only its commands can be set, not {bot_username}'s.")
        for i, c in enumerate(commands):
            if not c.get("command") or not c.get("description"):
                raise ValidationError(f"commands[{i}] needs command and description.")
        await client(functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(),
            lang_code="en",  # 默认语言；别的语言的菜单不动
            commands=[types.BotCommand(command=c["command"], description=c["description"]) for c in commands],
        ))
        return f"Set {len(commands)} command(s) for {bot_username}."
    except Exception as e:
        return log_and_format_error("set_bot_commands", e, bot_username=bot_username)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="Get Sticker Sets", openWorldHint=True, readOnlyHint=True))
async def get_sticker_sets() -> str:
    """List the sticker sets you have installed (title, short_name, count)."""
    try:
        res = await client(functions.messages.GetAllStickersRequest(hash=0))
        return dump_json([{"title": s.title, "short_name": s.short_name, "count": s.count} for s in res.sets])
    except Exception as e:
        return log_and_format_error("get_sticker_sets", e)
