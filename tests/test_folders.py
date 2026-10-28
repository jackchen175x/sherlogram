import json
import os

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

from types import SimpleNamespace

import pytest
from telethon import types

from tg_mcp import folders


def peer(uid):
    return types.InputPeerUser(user_id=uid, access_hash=uid * 10)


def folder(fid, title="Work", include=(), pinned=(), exclude=(), **flags):
    return types.DialogFilter(
        id=fid, title=types.TextWithEntities(text=title, entities=[]), emoticon="B",
        pinned_peers=list(pinned), include_peers=list(include), exclude_peers=list(exclude), **flags,
    )


def shared(fid, title="Shared", include=()):
    return types.DialogFilterChatlist(id=fid, title=types.TextWithEntities(text=title, entities=[]),
                                      pinned_peers=[], include_peers=list(include), has_my_invites=True)


class FakeClient:
    """GetDialogFilters 返回预设分组；其余请求只记录。raise_on 可让某个请求抛错。"""

    def __init__(self, *filters, raise_on=None):
        self.filters = [types.DialogFilterDefault(), *filters]
        self.calls = []
        self.raise_on = raise_on or {}

    async def __call__(self, request):
        self.calls.append(request)
        name = type(request).__name__
        if name in self.raise_on:
            raise self.raise_on[name]
        return SimpleNamespace(filters=self.filters) if name == "GetDialogFiltersRequest" else None

    def writes(self):
        return [c for c in self.calls if type(c).__name__ != "GetDialogFiltersRequest"]


def use(monkeypatch, fake):
    async def fake_input(chat_id):
        if chat_id == "ghost_user":
            raise ValueError("Cannot find any entity")
        return peer(int(chat_id))

    async def fake_resolve(identifier):
        if identifier == 3:
            raise ValueError("unknown")
        return SimpleNamespace(id=identifier, first_name=f"U{identifier}", username=f"u{identifier}")

    monkeypatch.setattr(folders, "client", fake)
    monkeypatch.setattr(folders, "resolve_input_entity", fake_input)
    monkeypatch.setattr(folders, "resolve_entity", fake_resolve)


# ---------- list_folders ----------


async def test_list_folders_skips_default_and_shows_only_set_flags(monkeypatch):
    fake = FakeClient(folder(2, include=[peer(1), peer(2)], pinned=[peer(1)], groups=True, exclude_muted=True), shared(5))
    use(monkeypatch, fake)
    out = json.loads(await folders.list_folders())
    assert out["count"] == 2
    work, sh = out["folders"]
    assert work == {"id": 2, "title": "Work", "emoticon": "B", "chats": 2, "pinned": 1, "excluded": 0,
                    "flags": ["groups", "exclude_muted"]}
    assert sh["shared"] is True and "flags" not in sh


async def test_list_folders_detail_resolves_chats(monkeypatch):
    fake = FakeClient(folder(2, include=[peer(1), peer(3)], pinned=[peer(1)], exclude=[peer(4)]))
    use(monkeypatch, fake)
    out = json.loads(await folders.list_folders(folder_id=2))
    assert out["chats"][0] == {"id": 1, "name": "U1", "type": "SimpleNamespace", "username": "u1"}
    assert out["chats"][1] == {"id": 3, "name": "Unknown"}  # 解析不出来的聊天不能拖垮整个输出
    assert out["pinned"][0]["id"] == 1 and out["excluded"][0]["id"] == 4


async def test_list_folders_detail_not_found(monkeypatch):
    use(monkeypatch, FakeClient(folder(2)))
    with pytest.raises(RuntimeError, match="Folder 9 not found"):
        await folders.list_folders(folder_id=9)


# ---------- manage_folder ----------


async def test_create_folder_takes_lowest_free_id(monkeypatch):
    fake = FakeClient(folder(2), folder(4))
    use(monkeypatch, fake)
    out = json.loads(await folders.manage_folder(action="create", title="New", emoticon="X", chat_ids=[7, 8], groups=True))
    assert out == {"folder_id": 3, "title": "New", "chats": 2}
    req = fake.writes()[0]
    assert req.id == 3 and req.filter.title.text == "New" and req.filter.groups is True
    assert [p.user_id for p in req.filter.include_peers] == [7, 8] and req.filter.pinned_peers == []


async def test_create_folder_pinned(monkeypatch):
    fake = FakeClient()
    use(monkeypatch, fake)
    await folders.manage_folder(action="create", title="P", chat_ids=[7], pinned=True)
    assert [p.user_id for p in fake.writes()[0].filter.pinned_peers] == [7]


async def test_create_folder_needs_title(monkeypatch):
    use(monkeypatch, FakeClient())
    with pytest.raises(RuntimeError, match="requires: title"):
        await folders.manage_folder(action="create")


async def test_create_folder_unresolvable_chat(monkeypatch):
    fake = FakeClient()
    use(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="Failed to resolve chat 'ghost_user'"):
        await folders.manage_folder(action="create", title="N", chat_ids=["ghost_user"])
    assert fake.writes() == []


async def test_create_folder_limit_message(monkeypatch):
    fake = FakeClient(raise_on={"UpdateDialogFilterRequest": RuntimeError("DIALOG_FILTERS_TOO_MUCH")})
    use(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="Premium"):
        await folders.manage_folder(action="create", title="N")


async def test_delete_folder(monkeypatch):
    fake = FakeClient(folder(2, title="Old"))
    use(monkeypatch, fake)
    out = await folders.manage_folder(action="delete", folder_id=2)
    assert "'Old'" in out
    req = fake.writes()[0]
    assert req.id == 2 and req.filter is None


async def test_delete_folder_refuses_system_and_missing(monkeypatch):
    fake = FakeClient(folder(2))
    use(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="system folder"):
        await folders.manage_folder(action="delete", folder_id=1)
    with pytest.raises(RuntimeError, match="not found"):
        await folders.manage_folder(action="delete", folder_id=7)
    assert fake.writes() == []


async def test_add_chat_keeps_other_fields(monkeypatch):
    fake = FakeClient(folder(2, include=[peer(1)], exclude=[peer(9)], groups=True, exclude_archived=True))
    use(monkeypatch, fake)
    out = await folders.manage_folder(action="add_chat", folder_id=2, chat_ids=[1, 5], pinned=True)
    assert "2 chat(s) added" in out and "pinned" in out
    new = fake.writes()[0].filter
    assert [p.user_id for p in new.include_peers] == [1, 5]  # 1 已在里面，不重复加
    assert [p.user_id for p in new.pinned_peers] == [1, 5]
    # 没改的设置必须原样带过去，否则整体覆盖时会被清掉
    assert [p.user_id for p in new.exclude_peers] == [9] and new.groups is True and new.exclude_archived is True
    assert new.emoticon == "B"


async def test_add_chat_is_idempotent(monkeypatch):
    fake = FakeClient(folder(2, include=[peer(1)]))
    use(monkeypatch, fake)
    out = await folders.manage_folder(action="add_chat", folder_id=2, chat_ids=[1])
    assert "Nothing to do" in out and fake.writes() == []


async def test_add_chat_to_shared_folder(monkeypatch):
    fake = FakeClient(shared(5, include=[peer(1)]))
    use(monkeypatch, fake)
    await folders.manage_folder(action="add_chat", folder_id=5, chat_ids=[2])
    new = fake.writes()[0].filter
    assert isinstance(new, types.DialogFilterChatlist) and new.has_my_invites is True
    assert [p.user_id for p in new.include_peers] == [1, 2]


async def test_remove_chat_from_include_and_pinned(monkeypatch):
    fake = FakeClient(folder(2, include=[peer(1), peer(2)], pinned=[peer(1)]))
    use(monkeypatch, fake)
    out = await folders.manage_folder(action="remove_chat", folder_id=2, chat_ids=[1])
    assert "removed from" in out
    new = fake.writes()[0].filter
    assert [p.user_id for p in new.include_peers] == [2] and new.pinned_peers == []


async def test_remove_chat_not_in_folder(monkeypatch):
    fake = FakeClient(folder(2, include=[peer(1)]))
    use(monkeypatch, fake)
    out = await folders.manage_folder(action="remove_chat", folder_id=2, chat_ids=[8])
    assert "Nothing to do" in out and fake.writes() == []


async def test_reorder_folders(monkeypatch):
    fake = FakeClient(folder(2), folder(3), shared(4))
    use(monkeypatch, fake)
    await folders.manage_folder(action="reorder", folder_ids=[4, 2, 3])
    assert fake.writes()[0].order == [4, 2, 3]


async def test_reorder_must_list_every_folder(monkeypatch):
    fake = FakeClient(folder(2), folder(3))
    use(monkeypatch, fake)
    with pytest.raises(RuntimeError, match=r"Unknown: \[9\], missing: \[3\]"):
        await folders.manage_folder(action="reorder", folder_ids=[2, 9])
    assert fake.writes() == []


async def test_manage_folder_unknown_action(monkeypatch):
    use(monkeypatch, FakeClient())
    with pytest.raises(RuntimeError, match="Unknown action: rename"):
        await folders.manage_folder(action="rename")


async def test_manage_folder_requires_params_per_action(monkeypatch):
    use(monkeypatch, FakeClient(folder(2)))
    with pytest.raises(RuntimeError, match="requires: folder_id, chat_ids"):
        await folders.manage_folder(action="add_chat")
    with pytest.raises(RuntimeError, match="Invalid chat_ids"):
        await folders.manage_folder(action="add_chat", folder_id=2, chat_ids=["bad name!"])


async def test_input_peers_turns_self_into_numbered_peer(monkeypatch):
    from telethon import types

    async def fake_input(ref):
        return types.InputPeerSelf()

    async def fake_resolve(ref):
        assert ref == "me"
        return types.User(id=777, access_hash=1)

    monkeypatch.setattr(folders, "resolve_input_entity", fake_input)
    monkeypatch.setattr(folders, "resolve_entity", fake_resolve)
    peers = await folders._input_peers(["me"])
    assert isinstance(peers[0], types.InputPeerUser) and peers[0].user_id == 777


async def test_remove_last_chat_is_refused(monkeypatch):
    from telethon import types

    peer = types.InputPeerUser(5, 1)
    f = types.DialogFilter(id=9, title="x", pinned_peers=[], include_peers=[peer], exclude_peers=[])

    class Fake:
        async def __call__(self, request):
            return SimpleNamespace(filters=[f])

    async def fake_input(ref):
        return peer

    monkeypatch.setattr(folders, "client", Fake())
    monkeypatch.setattr(folders, "resolve_input_entity", fake_input)
    with pytest.raises(RuntimeError, match="at least one chat"):
        await folders.manage_folder(action="remove_chat", folder_id=9, chat_ids=[5])
