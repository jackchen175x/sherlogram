import json
import os

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

from types import SimpleNamespace

import pytest
from telethon import types

import tg_mcp.app
from tg_mcp import me


class FakeClient:
    """按请求类名返回预设结果，并记下每个请求；get_me / upload_file 单独模拟。"""

    def __init__(self, me_user=None, **responses):
        self.me_user = me_user
        self.responses = responses
        self.calls = []

    async def __call__(self, request):
        self.calls.append(request)
        return self.responses.get(type(request).__name__)

    async def get_me(self):
        return self.me_user

    async def upload_file(self, path):
        self.uploaded = path
        return "UPLOADED"


def use(monkeypatch, fake):
    async def fake_resolve(identifier):
        if identifier == "ghost_user":
            raise ValueError("Cannot find any entity")
        return types.User(id=int(identifier), access_hash=int(identifier) * 10)

    monkeypatch.setattr(me, "client", fake)
    monkeypatch.setattr(me, "resolve_entity", fake_resolve)


# ---------- get_me ----------


async def test_get_me(monkeypatch):
    use(monkeypatch, FakeClient(SimpleNamespace(id=1, first_name="Jack", last_name=None, username="jack", phone="86139")))
    out = json.loads(await me.get_me())
    assert out == {"id": 1, "name": "Jack", "type": "user", "phone": "86139", "username": "jack"}


# ---------- update_profile ----------


async def test_update_profile_text_only_sends_given_fields(monkeypatch):
    fake = FakeClient()
    use(monkeypatch, fake)
    out = await me.update_profile(about="hello")
    req = fake.calls[0]
    assert (req.first_name, req.last_name, req.about) == (None, None, "hello")  # None = Telegram 不改
    assert "about" in out and len(fake.calls) == 1


async def test_update_profile_empty_about_clears_it(monkeypatch):
    fake = FakeClient()
    use(monkeypatch, fake)
    await me.update_profile(about="")
    assert fake.calls[0].about == ""


async def test_update_profile_photo(monkeypatch, tmp_path):
    monkeypatch.setattr(tg_mcp.app, "ALLOWED_ROOTS", [tmp_path])
    img = tmp_path / "me.jpg"
    img.write_bytes(b"x")
    fake = FakeClient()
    use(monkeypatch, fake)
    out = await me.update_profile(photo_path=str(img))
    assert fake.uploaded == str(img.resolve())
    assert fake.calls[0].file == "UPLOADED" and "photo" in out


async def test_update_profile_photo_outside_allowed_roots(monkeypatch, tmp_path):
    allowed = tmp_path / "ok"
    allowed.mkdir()
    monkeypatch.setattr(tg_mcp.app, "ALLOWED_ROOTS", [allowed])
    img = tmp_path / "me.jpg"
    img.write_bytes(b"x")
    fake = FakeClient()
    use(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="outside allowed roots"):
        await me.update_profile(photo_path=str(img))
    assert fake.calls == []


async def test_update_profile_photo_wrong_extension(monkeypatch, tmp_path):
    monkeypatch.setattr(tg_mcp.app, "ALLOWED_ROOTS", [tmp_path])
    doc = tmp_path / "me.pdf"
    doc.write_bytes(b"x")
    use(monkeypatch, FakeClient())
    with pytest.raises(RuntimeError, match="extension is not allowed"):
        await me.update_profile(photo_path=str(doc))


async def test_update_profile_delete_photo(monkeypatch):
    photo = SimpleNamespace(id=5)
    fake = FakeClient(GetUserPhotosRequest=SimpleNamespace(photos=[photo]))
    use(monkeypatch, fake)
    out = await me.update_profile(delete_photo=True)
    assert fake.calls[1].id == [photo] and "deleted" in out


async def test_update_profile_delete_photo_when_none(monkeypatch):
    fake = FakeClient(GetUserPhotosRequest=SimpleNamespace(photos=[]))
    use(monkeypatch, fake)
    out = await me.update_profile(delete_photo=True)
    assert len(fake.calls) == 1 and "none to delete" in out


async def test_update_profile_needs_something(monkeypatch):
    use(monkeypatch, FakeClient())
    with pytest.raises(RuntimeError, match="Nothing to update"):
        await me.update_profile()
    with pytest.raises(RuntimeError, match="not both"):
        await me.update_profile(photo_path="a.jpg", delete_photo=True)


# ---------- privacy ----------


async def test_privacy_get_defaults_to_last_seen(monkeypatch):
    rules = [types.PrivacyValueAllowAll(), types.PrivacyValueDisallowUsers(users=[7, 8])]
    fake = FakeClient(GetPrivacyRequest=SimpleNamespace(rules=rules))
    use(monkeypatch, fake)
    out = json.loads(await me.privacy())
    assert isinstance(fake.calls[0].key, types.InputPrivacyKeyStatusTimestamp)
    assert out == {"key": "status", "rules": [{"rule": "AllowAll"}, {"rule": "DisallowUsers", "ids": [7, 8]}]}


async def test_privacy_get_other_key(monkeypatch):
    fake = FakeClient(GetPrivacyRequest=SimpleNamespace(rules=[]))
    use(monkeypatch, fake)
    await me.privacy(action="get", key="phone")
    assert isinstance(fake.calls[0].key, types.InputPrivacyKeyPhoneNumber)


async def test_privacy_set_allow_and_disallow(monkeypatch):
    fake = FakeClient()
    use(monkeypatch, fake)
    out = await me.privacy(action="set", key="status", allow_users=[3, 4], disallow_users=["5"])
    req = fake.calls[0]
    assert isinstance(req.key, types.InputPrivacyKeyStatusTimestamp)
    allow, disallow = req.rules
    # 规则里必须是 InputUser，不能是原始 User 实体（嵌套对象 Telethon 不会替我们转）
    assert [u.user_id for u in allow.users] == [3, 4] and isinstance(allow.users[0], types.InputUser)
    assert [u.user_id for u in disallow.users] == [5]  # "5" 被校验装饰器转成了整数
    assert "only 2 user(s)" in out and "hidden from 1" in out


async def test_privacy_set_without_allow_list_means_everyone(monkeypatch):
    fake = FakeClient()
    use(monkeypatch, fake)
    out = await me.privacy(action="set", key="profile_photo", disallow_users=[9])
    allow, disallow = fake.calls[0].rules
    assert isinstance(allow, types.InputPrivacyValueAllowAll) and disallow.users[0].user_id == 9
    assert isinstance(fake.calls[0].key, types.InputPrivacyKeyProfilePhoto) and "everyone" in out


async def test_privacy_set_fails_closed_on_unresolvable_user(monkeypatch):
    fake = FakeClient()
    use(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="Nothing was changed"):
        await me.privacy(action="set", key="status", disallow_users=[3, "ghost_user"])
    assert fake.calls == []  # 一个都解析不出来就一个都不改，不能漏掉要拉黑的人


async def test_privacy_bad_key_and_action(monkeypatch):
    use(monkeypatch, FakeClient())
    with pytest.raises(RuntimeError, match="key must be one of"):
        await me.privacy(action="set")
    with pytest.raises(RuntimeError, match="key must be one of"):
        await me.privacy(action="get", key="bio")
    with pytest.raises(RuntimeError, match="Unknown action: reset"):
        await me.privacy(action="reset", key="status")


# ---------- set_bot_commands ----------


async def test_set_bot_commands(monkeypatch):
    fake = FakeClient(me_user=SimpleNamespace(bot=True, username="MyHelperBot"))
    use(monkeypatch, fake)
    out = await me.set_bot_commands(bot_username="@myhelperbot", commands=[{"command": "start", "description": "Begin"}])
    req = fake.calls[0]
    assert [(c.command, c.description) for c in req.commands] == [("start", "Begin")]
    assert isinstance(req.scope, types.BotCommandScopeDefault) and "Set 1 command" in out


async def test_set_bot_commands_refuses_user_account(monkeypatch):
    fake = FakeClient(me_user=SimpleNamespace(bot=False, username="jack"))
    use(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="regular user account"):
        await me.set_bot_commands(bot_username="x_bot", commands=[{"command": "a", "description": "b"}])
    assert fake.calls == []


async def test_set_bot_commands_refuses_other_bot(monkeypatch):
    fake = FakeClient(me_user=SimpleNamespace(bot=True, username="mybot"))
    use(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="only its commands can be set"):
        await me.set_bot_commands(bot_username="otherbot", commands=[{"command": "a", "description": "b"}])


async def test_set_bot_commands_rejects_incomplete_entry(monkeypatch):
    fake = FakeClient(me_user=SimpleNamespace(bot=True, username="mybot"))
    use(monkeypatch, fake)
    with pytest.raises(RuntimeError, match=r"commands\[0\]"):
        await me.set_bot_commands(bot_username="mybot", commands=[{"command": "a"}])


# ---------- get_sticker_sets ----------


async def test_get_sticker_sets(monkeypatch):
    sets = [SimpleNamespace(title="Cats", short_name="cats_pack", count=30)]
    use(monkeypatch, FakeClient(GetAllStickersRequest=SimpleNamespace(sets=sets)))
    assert json.loads(await me.get_sticker_sets()) == [{"title": "Cats", "short_name": "cats_pack", "count": 30}]
