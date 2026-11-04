import os

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon import errors, types

import tg_mcp.app
from tg_mcp import chats

DATE = datetime(2026, 1, 2, tzinfo=timezone.utc)
CHANNEL = types.Channel(id=10, title="Shop", photo=None, date=DATE, access_hash=1, megagroup=True, forum=True)
BASIC = types.Chat(id=20, title="Basic", photo=None, participants_count=3, date=DATE, version=1)
USER = types.User(id=30, access_hash=3, first_name="Ann", username="ann_seller",
                  status=types.UserStatusOffline(was_online=DATE))
BOT = types.User(id=40, access_hash=4, first_name="Helper", bot=True, bot_active_users=1200)


class FakeClient:
    """按请求类名返回预设结果（值是异常就抛出），并记下每个请求。"""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.requests = []
        self.kwargs = {}
        self.dialogs, self.participants, self.messages = [], [], []

    async def __call__(self, request):
        self.requests.append(request)
        r = self.responses.get(type(request).__name__)
        if callable(r):
            r = r(request)
        if isinstance(r, Exception):
            raise r
        return r

    def sent(self, name):
        return [r for r in self.requests if type(r).__name__ == name]

    async def get_dialogs(self, limit=None):
        return self.dialogs

    async def get_participants(self, entity, **kwargs):
        self.kwargs = kwargs
        return self.participants

    async def get_messages(self, entity, limit=1):
        return self.messages

    async def upload_file(self, path):
        return f"uploaded:{path}"


@pytest.fixture
def fake(monkeypatch):
    f = FakeClient()
    entities = {10: CHANNEL, 20: BASIC, 30: USER, 40: BOT, "@ann_seller": USER, "@shop": CHANNEL, "shop_name": CHANNEL}

    async def fake_resolve(x):
        return entities[x]

    monkeypatch.setattr(chats, "client", f)
    monkeypatch.setattr(chats, "resolve_entity", fake_resolve)
    return f


# ---------- list_chats ----------


async def test_list_chats_filters(fake):
    muted = SimpleNamespace(notify_settings=SimpleNamespace(mute_until=2**31 - 1), unread_mark=False)
    fake.dialogs = [
        SimpleNamespace(entity=USER, unread_count=3, dialog=SimpleNamespace(notify_settings=None, unread_mark=False)),
        SimpleNamespace(entity=CHANNEL, unread_count=0, dialog=muted),
    ]
    out = await chats.list_chats()
    assert "Chat ID: 30, Name: Ann, Type: User, Username: @ann_seller, Unread: 3, Muted: no" in out
    assert "Title: Shop" in out and "Muted: yes" in out
    assert "Shop" not in await chats.list_chats(unread_only=True)
    assert "Ann" not in await chats.list_chats(chat_type="group")


# ---------- get_chat ----------


async def test_get_chat_user_details(fake):
    fake.responses = {
        "GetFullUserRequest": SimpleNamespace(
            users=[USER], full_user=SimpleNamespace(about="sells gpt", common_chats_count=1, bot_info=None)
        ),
        "GetCommonChatsRequest": SimpleNamespace(chats=[SimpleNamespace(id=5, title="Market")]),
        "GetUserPhotosRequest": SimpleNamespace(photos=[SimpleNamespace(id=77, date=DATE)]),
        "GetPeerDialogsRequest": SimpleNamespace(dialogs=[SimpleNamespace(unread_count=4)]),
    }
    out = await chats.get_chat(30, include_photos=True)
    assert "Bio: sells gpt" in out
    assert "Status: offline, last seen 2026-01-02" in out
    assert "Common chat list: 5 Market" in out
    assert "Photos: 77 (2026-01-02)" in out
    assert "Unread Messages: 4" in out


async def test_get_chat_bot_commands(fake):
    bot_info = SimpleNamespace(description="I help", commands=[types.BotCommand("start", "Begin")])
    fake.responses = {
        "GetFullUserRequest": SimpleNamespace(
            users=[BOT], full_user=SimpleNamespace(about=None, common_chats_count=0, bot_info=bot_info)
        )
    }
    out = await chats.get_chat(40)
    assert "Type: Bot" in out and "Bot description: I help" in out
    assert "Bot commands: /start - Begin" in out and "Bot monthly users: 1200" in out
    assert "Status:" not in out and not fake.sent("GetUserPhotosRequest")


async def test_get_chat_group_opacity(fake):
    fake.participants = SimpleNamespace(total=99)
    fake.responses = {"GetFullChannelRequest": SimpleNamespace(full_chat=SimpleNamespace(hidden_prehistory=True))}
    out = await chats.get_chat(10)
    assert "Participants: 99" in out and "Opacity (1)" in out


# ---------- get_participants ----------


async def test_get_participants_role(fake):
    admin = SimpleNamespace(id=1, first_name="Boss", last_name="", username="boss",
                            participant=types.ChannelParticipantCreator(user_id=1, admin_rights=types.ChatAdminRights()))
    fake.participants = [admin]
    out = await chats.get_participants(10, role="admin")
    assert fake.kwargs["filter"] is types.ChannelParticipantsAdmins
    assert '"role":"creator"' in out.replace(" ", "") and '"username":"boss"' in out.replace(" ", "")
    await chats.get_participants(10, role="all")
    assert "filter" not in fake.kwargs
    with pytest.raises(RuntimeError, match="Unknown role"):
        await chats.get_participants(10, role="owners")


# ---------- list_topics ----------


async def test_list_topics(fake):
    fake.responses = {"GetForumTopicsRequest": SimpleNamespace(
        topics=[SimpleNamespace(id=3, title="Prices", top_message=9, unread_count=2, closed=False)],
        messages=[SimpleNamespace(id=9, date=DATE)],
    )}
    out = await chats.list_topics(10, search_query="pri")
    assert '"title":"Prices"' in out.replace(" ", "") and "2026-01-02" in out
    assert fake.sent("GetForumTopicsRequest")[0].q == "pri"
    assert "not a supergroup" in await chats.list_topics(20)


# ---------- create_chat ----------


async def test_create_chat_group(fake):
    fake.responses = {"CreateChatRequest": SimpleNamespace(
        updates=SimpleNamespace(chats=[SimpleNamespace(id=50)]), missing_invitees=[])}
    assert "ID: 50" in await chats.create_chat("New", user_ids=[30])
    with pytest.raises(RuntimeError, match="needs at least one user"):
        await chats.create_chat("New")


async def test_create_chat_supergroup_invites(fake):
    fake.responses = {
        "CreateChannelRequest": SimpleNamespace(chats=[CHANNEL]),
        "InviteToChannelRequest": SimpleNamespace(missing_invitees=[SimpleNamespace(user_id=30)]),
    }
    out = await chats.create_chat("Sg", kind="supergroup", user_ids=["@ann_seller"])
    req = fake.sent("CreateChannelRequest")[0]
    assert req.megagroup and not req.broadcast
    assert "Could not add: [30]" in out


async def test_create_chat_channel_and_bad_kind(fake):
    fake.responses = {"CreateChannelRequest": SimpleNamespace(chats=[CHANNEL])}
    await chats.create_chat("Ch", kind="channel")
    assert fake.sent("CreateChannelRequest")[0].broadcast
    with pytest.raises(RuntimeError, match="Unknown kind"):
        await chats.create_chat("X", kind="forum")


# ---------- edit_chat ----------


async def test_edit_chat_title_and_photo(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(tg_mcp.app, "ALLOWED_ROOTS", [tmp_path])
    (tmp_path / "p.jpg").write_bytes(b"x")
    out = await chats.edit_chat(10, title="Renamed", photo_path="p.jpg")
    assert fake.sent("EditTitleRequest")[0].title == "Renamed"
    assert isinstance(fake.sent("EditPhotoRequest")[0].photo, types.InputChatUploadedPhoto)
    assert "photo updated" in out


async def test_edit_chat_delete_photo_basic_group(fake):
    await chats.edit_chat(20, delete_photo=True)
    assert isinstance(fake.sent("EditChatPhotoRequest")[0].photo, types.InputChatPhotoEmpty)


async def test_edit_chat_validation(fake, tmp_path, monkeypatch):
    (tmp_path / "allowed").mkdir()
    (tmp_path / "outside.jpg").write_bytes(b"x")
    monkeypatch.setattr(tg_mcp.app, "ALLOWED_ROOTS", [tmp_path / "allowed"])
    with pytest.raises(RuntimeError, match="at least one"):
        await chats.edit_chat(10)
    with pytest.raises(RuntimeError, match="outside allowed roots"):
        await chats.edit_chat(10, photo_path=str(tmp_path / "outside.jpg"))
    assert not fake.requests  # 被拒的路径不能走到上传
    with pytest.raises(RuntimeError, match="only groups"):
        await chats.edit_chat(30, title="x")


async def test_edit_chat_username(fake):
    assert "username set to @shop_chat" in await chats.edit_chat(10, username="@shop_chat")
    assert fake.sent("UpdateUsernameRequest")[0].username == "shop_chat"
    assert "username removed" in await chats.edit_chat(10, username="")
    with pytest.raises(RuntimeError, match="basic group"):
        await chats.edit_chat(20, username="x_chat")
    fake.responses = {"UpdateUsernameRequest": errors.UsernameOccupiedError(request=None)}
    with pytest.raises(RuntimeError, match="already taken"):
        await chats.edit_chat(10, username="taken")


# ---------- manage_member ----------


@pytest.mark.parametrize("action, request_name", [
    ("invite", "InviteToChannelRequest"),
    ("ban", "EditBannedRequest"),
    ("unban", "EditBannedRequest"),
    ("promote", "EditAdminRequest"),
    ("demote", "EditAdminRequest"),
])
async def test_manage_member_actions(fake, action, request_name):
    out = await chats.manage_member(10, [30, "@ann_seller"], action)
    sent = fake.sent(request_name)
    assert len(sent) == 2 and '"action":"%s"' % action in out.replace(" ", "")
    if action in ("ban", "unban"):
        assert sent[0].banned_rights.view_messages is (action == "ban")
    if action == "demote":
        assert sent[0].rank == "" and not sent[0].admin_rights.ban_users


async def test_manage_member_promote_rights_override(fake):
    await chats.manage_member(10, 30, "promote", rights={"add_admins": True})
    rights = fake.sent("EditAdminRequest")[0].admin_rights
    assert rights.add_admins and rights.ban_users


async def test_manage_member_basic_group_and_errors(fake):
    await chats.manage_member(20, 30, "invite")
    assert fake.sent("AddChatUserRequest")[0].chat_id == 20
    fake.responses = {"InviteToChannelRequest": errors.UserPrivacyRestrictedError(request=None)}
    with pytest.raises(RuntimeError, match="privacy settings"):
        await chats.manage_member(10, 30, "invite")
    with pytest.raises(RuntimeError, match="Unknown action"):
        await chats.manage_member(10, 30, "kick")


# ---------- get_invite_link ----------


async def test_get_invite_link(fake):
    fake.responses = {
        "GetFullChannelRequest": SimpleNamespace(full_chat=SimpleNamespace(exported_invite=SimpleNamespace(link="https://t.me/+old"))),
        "ExportChatInviteRequest": SimpleNamespace(link="https://t.me/+new"),
    }
    assert await chats.get_invite_link(10) == "https://t.me/+old"
    assert not fake.sent("ExportChatInviteRequest")  # 读链接不该顺手造一条新的
    assert await chats.get_invite_link(10, new=True) == "https://t.me/+new"


# ---------- join_chat ----------


async def test_join_chat_public(fake):
    assert "Joined Shop" in await chats.join_chat("@shop")
    assert fake.sent("JoinChannelRequest")[0].channel is CHANNEL


@pytest.mark.parametrize("link", ["https://t.me/+AbCdEf123", "t.me/joinchat/AbCdEf123", "+AbCdEf123", "AbCdEf123"])
async def test_join_chat_invite_forms(fake, link):
    fake.responses = {
        "CheckChatInviteRequest": SimpleNamespace(title="Private"),
        "ImportChatInviteRequest": SimpleNamespace(chats=[SimpleNamespace(id=60, title="Private")]),
    }
    assert "Joined Private (ID: 60)" in await chats.join_chat(link)
    assert fake.sent("ImportChatInviteRequest")[0].hash == "AbCdEf123"


async def test_join_chat_bare_username_falls_back(fake):
    fake.responses = {"CheckChatInviteRequest": errors.InviteHashInvalidError(request=None)}
    assert "Joined Shop" in await chats.join_chat("shop_name")
    assert "Joined Shop" in await chats.join_chat("https://t.me/shop_name/123")


async def test_join_chat_bare_username_answered_as_expired(fake):
    # 真号实测：@ 不写时，服务器把用户名当邀请码查，回的是「过期」，不能原样报给用户
    fake.responses = {"CheckChatInviteRequest": errors.InviteHashExpiredError(request=None)}
    assert "Joined Shop" in await chats.join_chat("shop_name")


async def test_join_chat_unknown_name_says_so(fake, monkeypatch):
    async def not_found(_):
        raise ValueError("No user has that username")
    monkeypatch.setattr(chats, "resolve_entity", not_found)
    fake.responses = {"CheckChatInviteRequest": errors.InviteHashExpiredError(request=None)}
    with pytest.raises(RuntimeError, match="Cannot find that chat"):
        await chats.join_chat("no_such_name")


async def test_join_chat_already_and_expired(fake):
    fake.responses = {"CheckChatInviteRequest": types.ChatInviteAlready(chat=SimpleNamespace(id=61, title="Old"))}
    assert "Already a member of Old" in await chats.join_chat("t.me/+zzz")
    fake.responses = {"CheckChatInviteRequest": errors.InviteHashExpiredError(request=None)}
    with pytest.raises(RuntimeError, match="expired"):
        await chats.join_chat("t.me/+zzz")


# ---------- leave_chat ----------


async def test_leave_chat(fake):
    assert "Left Shop" in await chats.leave_chat(10)
    assert fake.sent("LeaveChannelRequest")
    await chats.leave_chat(20)
    assert isinstance(fake.sent("DeleteChatUserRequest")[0].user_id, types.InputUserSelf)
    with pytest.raises(RuntimeError, match="only groups/channels"):
        await chats.leave_chat(30)


# ---------- get_recent_actions ----------


async def test_get_recent_actions(fake):
    deleted = types.ChannelAdminLogEventActionDeleteMessage(message=SimpleNamespace(message="差评原文"))
    renamed = types.ChannelAdminLogEventActionChangeTitle(prev_value="Old", new_value="New")
    fake.responses = {"GetAdminLogRequest": SimpleNamespace(
        events=[SimpleNamespace(id=1, date=DATE, user_id=30, action=deleted),
                SimpleNamespace(id=2, date=DATE, user_id=99, action=renamed)],
        users=[USER],
    )}
    out = (await chats.get_recent_actions(10)).replace(" ", "")
    assert '"by":"Ann","action":"DeleteMessage","text":"差评原文"' in out
    assert '"prev_value":"Old","new_value":"New"' in out


# ---------- set_chat_state ----------


@pytest.mark.parametrize("action, request_name", [
    ("mute", "UpdateNotifySettingsRequest"),
    ("unmute", "UpdateNotifySettingsRequest"),
    ("archive", "EditPeerFoldersRequest"),
    ("unarchive", "EditPeerFoldersRequest"),
])
async def test_set_chat_state(fake, action, request_name):
    await chats.set_chat_state(10, action)
    req = fake.sent(request_name)[0]
    if request_name == "UpdateNotifySettingsRequest":
        assert req.settings.mute_until == (2**31 - 1 if action == "mute" else 0)
    else:
        assert req.folder_peers[0].folder_id == (1 if action == "archive" else 0)


async def test_set_chat_state_unknown(fake):
    with pytest.raises(RuntimeError, match="Unknown action"):
        await chats.set_chat_state(10, "pin")
