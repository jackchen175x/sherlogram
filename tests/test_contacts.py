import json
import os

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

from types import SimpleNamespace

import pytest
from telethon import types

from tg_mcp import contacts


class FakeClient:
    """按请求类名返回预设结果，并记下每个请求，断言时看参数。"""

    def __init__(self, **responses):
        self.responses = responses
        self.calls = []

    async def __call__(self, request):
        self.calls.append(request)
        return self.responses.get(type(request).__name__)


ANN = types.User(id=1, first_name="Ann", last_name="Lee", username="ann_lee", phone="8613800000000", contact=True)
STRANGER = types.User(id=2, first_name="Bob")


def use(monkeypatch, fake, entity=None):
    async def fake_resolve(identifier):
        fake.resolved = identifier
        return entity

    monkeypatch.setattr(contacts, "client", fake)
    monkeypatch.setattr(contacts, "resolve_entity", fake_resolve)


# ---------- list_contacts ----------


async def test_list_contacts_all(monkeypatch):
    fake = FakeClient(GetContactsRequest=SimpleNamespace(users=[ANN, STRANGER]))
    use(monkeypatch, fake)
    out = json.loads(await contacts.list_contacts())
    assert out["count"] == 2
    # id 直接能当 chat_id 用；空字段不输出
    assert out["contacts"][0] == {"id": 1, "name": "Ann Lee", "username": "ann_lee", "phone": "8613800000000"}
    assert out["contacts"][1] == {"id": 2, "name": "Bob"}


async def test_list_contacts_query_marks_real_contacts(monkeypatch):
    fake = FakeClient(SearchRequest=SimpleNamespace(users=[ANN, STRANGER]))
    use(monkeypatch, fake)
    out = json.loads(await contacts.list_contacts(query="ann"))
    assert fake.calls[0].q == "ann"
    assert out["contacts"][0]["contact"] is True
    assert "contact" not in out["contacts"][1]


async def test_list_contacts_blocked_only(monkeypatch):
    fake = FakeClient(GetBlockedRequest=SimpleNamespace(users=[STRANGER], count=250))
    use(monkeypatch, fake)
    out = json.loads(await contacts.list_contacts(blocked_only=True))
    assert out == {"count": 250, "blocked": [{"id": 2, "name": "Bob"}]}  # count 比条数大 = 被截断


async def test_list_contacts_empty(monkeypatch):
    use(monkeypatch, FakeClient(GetContactsRequest=SimpleNamespace(users=[])))
    assert json.loads(await contacts.list_contacts()) == {"count": 0, "contacts": []}


# ---------- manage_contact ----------


async def test_add_by_phone(monkeypatch):
    fake = FakeClient(ImportContactsRequest=SimpleNamespace(imported=[object()]))
    use(monkeypatch, fake)
    out = await contacts.manage_contact(action="add", phone="+8613900000000", first_name="Cat")
    assert "added" in out
    c = fake.calls[0].contacts[0]
    assert (c.phone, c.first_name, c.last_name) == ("+8613900000000", "Cat", "")


async def test_add_by_phone_not_on_telegram(monkeypatch):
    use(monkeypatch, FakeClient(ImportContactsRequest=SimpleNamespace(imported=[])))
    out = await contacts.manage_contact(action="add", phone="+8613900000000", first_name="Cat")
    assert "did not import" in out


async def test_add_by_phone_needs_first_name(monkeypatch):
    use(monkeypatch, FakeClient())
    with pytest.raises(RuntimeError, match="first_name"):
        await contacts.manage_contact(action="add", phone="+8613900000000")


async def test_add_by_username_resolves_user(monkeypatch):
    fake = FakeClient(AddContactRequest=SimpleNamespace(updates=[]))
    use(monkeypatch, fake, entity=STRANGER)
    out = await contacts.manage_contact(action="add", username="@bobby_b")
    assert fake.resolved == "bobby_b"  # @ 被去掉
    req = fake.calls[0]
    assert req.id is STRANGER and req.first_name == "Bob" and req.phone == ""  # 没给名字就用对方自己的
    assert "added" in out


async def test_add_rejects_non_user(monkeypatch):
    use(monkeypatch, FakeClient(), entity=types.Channel(id=9, title="C", photo=None, date=None))
    with pytest.raises(RuntimeError, match="not a user"):
        await contacts.manage_contact(action="add", user_id=9)


async def test_add_without_any_identifier(monkeypatch):
    use(monkeypatch, FakeClient())
    with pytest.raises(RuntimeError, match="phone, username or user_id"):
        await contacts.manage_contact(action="add")


async def test_delete_contact(monkeypatch):
    fake = FakeClient()
    use(monkeypatch, fake, entity=ANN)
    out = await contacts.manage_contact(action="delete", user_id="@ann_lee")
    assert fake.calls[0].id == [ANN] and "deleted" in out


async def test_block_and_unblock(monkeypatch):
    fake = FakeClient()
    use(monkeypatch, fake, entity=ANN)
    assert "blocked" in await contacts.manage_contact(action="block", user_id=1)
    assert "unblocked" in await contacts.manage_contact(action="unblock", user_id=1)
    assert [type(c).__name__ for c in fake.calls] == ["BlockRequest", "UnblockRequest"]
    assert fake.calls[0].id is ANN


async def test_delete_needs_user_id(monkeypatch):
    use(monkeypatch, FakeClient())
    with pytest.raises(RuntimeError, match="requires: user_id"):
        await contacts.manage_contact(action="delete")


async def test_import_contacts(monkeypatch):
    fake = FakeClient(ImportContactsRequest=SimpleNamespace(imported=[object()]))
    use(monkeypatch, fake)
    out = json.loads(await contacts.manage_contact(action="import", contacts=[
        {"phone": "+111", "first_name": "A", "last_name": "B"},
        {"phone": "+222", "first_name": "C"},
    ]))
    assert out == {"submitted": 2, "imported": 1}
    sent = fake.calls[0].contacts
    assert [(c.client_id, c.phone, c.last_name) for c in sent] == [(0, "+111", "B"), (1, "+222", "")]


async def test_import_rejects_incomplete_entry(monkeypatch):
    fake = FakeClient()
    use(monkeypatch, fake)
    with pytest.raises(RuntimeError, match=r"contacts\[1\]"):
        await contacts.manage_contact(action="import", contacts=[{"phone": "+1", "first_name": "A"}, {"phone": "+2"}])
    assert fake.calls == []  # 有一条不合格就一条都不发


async def test_manage_contact_unknown_action(monkeypatch):
    use(monkeypatch, FakeClient())
    with pytest.raises(RuntimeError, match="Unknown action: nuke"):
        await contacts.manage_contact(action="nuke")


async def test_manage_contact_rejects_bad_user_id(monkeypatch):
    use(monkeypatch, FakeClient())
    with pytest.raises(RuntimeError, match="Invalid user_id"):
        await contacts.manage_contact(action="block", user_id="no good!")
