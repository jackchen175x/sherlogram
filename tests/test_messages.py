import os

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import functions, types

import tg_mcp.app
from tg_mcp import messages

DT = datetime(2026, 10, 1, tzinfo=timezone.utc)


class MessageMediaDocument:  # 只要类名对：媒体摘要按类名出类型
    pass


def mk(id, text="hi", **kw):
    base = dict(id=id, message=text, date=DT, sender=SimpleNamespace(first_name="Ann", username="ann_seller"),
                reply_to=None, media=None, file=None, reply_markup=None)
    base.update(kw)
    return SimpleNamespace(**base)


def markup(*buttons):
    return types.ReplyInlineMarkup(rows=[types.KeyboardButtonRow(buttons=list(buttons))])


class FakeClient:
    def __init__(self):
        self.msgs, self.result, self.calls = [], None, []

    def _rec(self, name, *args, **kw):
        self.calls.append((name, args, kw))

    async def get_messages(self, entity, ids=None, limit=None, **kw):
        self._rec("get_messages", ids=ids, limit=limit, **kw)
        if ids is not None:
            return next((m for m in self.msgs if m.id == ids), None)
        if "max_id" in kw:
            return sorted((m for m in self.msgs if m.id < kw["max_id"]), key=lambda m: -m.id)[:limit]
        if "min_id" in kw:
            return sorted((m for m in self.msgs if m.id > kw["min_id"]), key=lambda m: m.id)[:limit]
        return self.msgs[:limit]

    async def iter_messages(self, entity, **kw):
        self._rec("iter_messages", **kw)
        for m in self.msgs:
            yield m

    async def send_message(self, entity, message, **kw):
        self._rec("send_message", entity, message, **kw)
        return SimpleNamespace(id=100)

    async def send_file(self, entity, file, **kw):
        self._rec("send_file", entity, file, **kw)
        return SimpleNamespace(id=101)

    async def edit_message(self, *a):
        self._rec("edit_message", *a)

    async def delete_messages(self, entity, ids):
        self._rec("delete_messages", entity, ids)
        return [SimpleNamespace(pts_count=len(ids) - 1)]

    async def forward_messages(self, *a):
        self._rec("forward_messages", *a)

    async def pin_message(self, *a):
        self._rec("pin_message", *a)

    async def unpin_message(self, *a):
        self._rec("unpin_message", *a)

    async def send_read_acknowledge(self, *a):
        self._rec("send_read_acknowledge", *a)

    async def download_media(self, msg, file):
        out = Path(file + ".pdf")
        out.write_bytes(b"%PDF")
        return str(out)

    async def __call__(self, request):
        self._rec("call", request)
        return self.result


@pytest.fixture
def fake(monkeypatch, tmp_path):
    c = FakeClient()
    monkeypatch.setattr(messages, "client", c)

    async def resolve(x):
        return f"E{x}"

    monkeypatch.setattr(messages, "resolve_entity", resolve)
    monkeypatch.setattr(tg_mcp.app, "ALLOWED_ROOTS", [tmp_path])
    return c


def last(c, name):
    return next(call for call in reversed(c.calls) if call[0] == name)


# ---------- list_messages / get_pinned_messages ----------


async def test_list_messages_shows_media_and_buttons(fake):
    media_msg = mk(2, "price list", media=MessageMediaDocument(),
                   file=SimpleNamespace(name="price.pdf", mime_type="application/pdf", size=2048),
                   reply_markup=markup(types.KeyboardButtonCallback(text="Buy", data=b"buy"),
                                       types.KeyboardButtonUrl(text="Shop", url="https://shop.example")))
    fake.msgs = [media_msg, mk(1)]
    out = await messages.list_messages(chat_id=1, limit=5)
    assert "media=[Document, price.pdf, application/pdf, 2048B]" in out
    assert '{"row":0,"col":0,"text":"Buy","type":"callback"}' in out.replace(" ", "")
    assert '"url":"https://shop.example"' in out.replace(" ", "")
    assert last(fake, "get_messages")[2]["limit"] == 5


async def test_list_messages_search_and_from_date(fake):
    fake.msgs = [mk(3)]
    await messages.list_messages(chat_id=1, search_query="usdt")
    assert last(fake, "iter_messages")[2]["search"] == "usdt"
    await messages.list_messages(chat_id=1, from_date="2026-09-01")
    kw = last(fake, "iter_messages")[2]
    assert kw["reverse"] is True and kw["offset_date"].day == 1


async def test_list_messages_bad_date(fake):
    with pytest.raises(RuntimeError, match="Invalid date format"):
        await messages.list_messages(chat_id=1, to_date="10/01")


async def test_list_messages_around(fake):
    fake.msgs = [mk(i) for i in range(1, 10)]
    out = await messages.list_messages(chat_id=1, limit=4, around_message_id=5)
    ids = [int(line.split(" | ")[0][4:]) for line in out.splitlines()]
    assert ids == [3, 4, 5, 6, 7]
    assert "ID: 5 | Ann (@ann_seller) | Date: 2026-10-01 00:00:00+00:00 [THIS MESSAGE]" in out


async def test_get_pinned_messages_fetches_more_than_one(fake):
    fake.msgs = [mk(1, "rules " * 200)]
    out = await messages.get_pinned_messages(chat_id=1)
    call = last(fake, "get_messages")[2]
    assert call["limit"] == 50 and isinstance(call["filter"], types.InputMessagesFilterPinned)
    assert "已截断" not in out  # 置顶不截断


# ---------- send_message 各分支 ----------


async def test_send_text_with_reply(fake):
    out = await messages.send_message(chat_id="123", message="hi", reply_to=9, parse_mode="html")
    _, args, kw = last(fake, "send_message")
    assert args == ("E123", "hi") and kw == {"reply_to": 9, "parse_mode": "html"}
    assert "100" in out


async def test_send_file_with_caption(fake, tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"x")
    await messages.send_message(chat_id=1, message="cap", file_path="a.pdf")
    _, args, kw = last(fake, "send_file")
    assert args[1] == str((tmp_path / "a.pdf").resolve()) and kw["caption"] == "cap" and kw["voice_note"] is False


async def test_send_voice_checks_extension(fake, tmp_path):
    (tmp_path / "v.ogg").write_bytes(b"x")
    (tmp_path / "v.txt").write_bytes(b"x")
    await messages.send_message(chat_id=1, file_path="v.ogg", as_voice=True)
    assert last(fake, "send_file")[2]["voice_note"] is True
    with pytest.raises(RuntimeError, match="not allowed for voice"):
        await messages.send_message(chat_id=1, file_path="v.txt", as_voice=True)


async def test_send_sticker(fake, tmp_path):
    (tmp_path / "s.webp").write_bytes(b"x")
    await messages.send_message(chat_id=1, file_path="s.webp")
    assert last(fake, "send_file")[1][1].endswith("s.webp")


async def test_send_file_outside_roots(fake):
    with pytest.raises(RuntimeError, match="outside allowed roots"):
        await messages.send_message(chat_id=1, file_path="/etc/hosts")


async def test_send_gif_from_saved(fake):
    doc = SimpleNamespace(id=7)
    fake.result = SimpleNamespace(gifs=[doc])
    messages.GIF_DOCS.clear()
    await messages.send_message(chat_id=1, gif_id=7)
    assert isinstance(last(fake, "call")[1][0], functions.messages.GetSavedGifsRequest)
    assert last(fake, "send_file")[1][1] is doc
    with pytest.raises(RuntimeError, match="run search_gifs first"):
        await messages.send_message(chat_id=1, gif_id=8)


async def test_send_gif_from_search_cache(fake):
    # search_gifs 搜到的文档先走缓存，不再去查收藏
    cached = SimpleNamespace(id=9)
    messages.GIF_DOCS[9] = cached
    fake.result = SimpleNamespace(gifs=[])
    await messages.send_message(chat_id=1, gif_id=9)
    assert last(fake, "send_file")[1][1] is cached
    messages.GIF_DOCS.clear()


async def test_send_nothing(fake):
    with pytest.raises(RuntimeError, match="Provide message"):
        await messages.send_message(chat_id=1)


async def test_send_contact(fake):
    await messages.send_contact(chat_id=1, phone="+100", first_name="Bob")
    media = last(fake, "send_file")[1][1]
    assert isinstance(media, types.InputMediaContact) and media.phone_number == "+100"


async def test_create_poll(fake):
    await messages.create_poll(chat_id=1, question="Q?", options=["a", "b", "c"])
    media = last(fake, "send_file")[1][1]
    assert len(media.poll.answers) == 3 and media.poll.public_voters is False and media.correct_answers is None


async def test_create_poll_quiz_and_validation(fake):
    await messages.create_poll(chat_id=1, question="Q?", options=["a", "b"], quiz_mode=True, correct_option=1)
    assert last(fake, "send_file")[1][1].correct_answers == [b"\x01"]
    with pytest.raises(RuntimeError, match="correct_option"):
        await messages.create_poll(chat_id=1, question="Q?", options=["a", "b"], quiz_mode=True)
    with pytest.raises(RuntimeError, match="2-10"):
        await messages.create_poll(chat_id=1, question="Q?", options=["a"])


# ---------- 改 / 删 / 转发 / 置顶 / 已读 ----------


async def test_edit_message(fake):
    await messages.edit_message(chat_id=1, message_id=5, new_text="new")
    assert last(fake, "edit_message")[1] == ("E1", 5, "new")


async def test_delete_many_reports_real_count(fake):
    out = await messages.delete_message(chat_id=1, message_ids=[4, 5])
    assert last(fake, "delete_messages")[1] == ("E1", [4, 5])
    assert out.startswith("Deleted 1 of 2")


async def test_forward_single(fake):
    out = await messages.forward_message(from_chat_id=1, message_ids=3, to_chat_id="@target_chat")
    assert last(fake, "forward_messages")[1] == ("E@target_chat", [3], "E1")
    assert "1 message" in out


async def test_pin_and_unpin(fake):
    await messages.pin_message(chat_id=1, message_id=5)
    assert last(fake, "pin_message")[1] == ("E1", 5)
    await messages.pin_message(chat_id=1, message_id=5, unpin=True)
    assert last(fake, "unpin_message")[1] == ("E1", 5)


async def test_mark_as_read(fake):
    await messages.mark_as_read(chat_id=1)
    assert last(fake, "send_read_acknowledge")[1] == ("E1",)


# ---------- react ----------


async def test_react_add(fake):
    await messages.react(chat_id=1, message_id=5, emoji="👍", big=True)
    req = last(fake, "call")[1][0]
    assert isinstance(req, functions.messages.SendReactionRequest) and req.reaction[0].emoticon == "👍" and req.big


async def test_react_remove(fake):
    await messages.react(chat_id=1, message_id=5, action="remove")
    assert last(fake, "call")[1][0].reaction == []


async def test_react_list(fake):
    fake.result = SimpleNamespace(reactions=[
        SimpleNamespace(peer_id=types.PeerUser(user_id=5), reaction=types.ReactionEmoji(emoticon="🔥"), date=DT),
        SimpleNamespace(peer_id=types.PeerChannel(channel_id=9), reaction=types.ReactionCustomEmoji(document_id=42), date=DT),
    ])
    out = await messages.react(chat_id=1, message_id=5, action="list")
    assert '"peer_id":5,"emoji":"🔥"' in out and "custom:42" in out and "-1000000000009" in out


async def test_react_bad_action_and_missing_emoji(fake):
    with pytest.raises(RuntimeError, match="Unknown action: zap"):
        await messages.react(chat_id=1, message_id=5, action="zap")
    with pytest.raises(RuntimeError, match="needs emoji"):
        await messages.react(chat_id=1, message_id=5)


# ---------- press_inline_button ----------


async def test_press_button_by_text(fake):
    fake.msgs = [mk(1), mk(2, reply_markup=markup(types.KeyboardButtonCallback(text="Buy", data=b"buy")))]
    fake.result = SimpleNamespace(message="Order placed", alert=False, url=None)
    out = await messages.press_inline_button(chat_id=1, button_text=" buy ")
    req = last(fake, "call")[1][0]
    assert isinstance(req, functions.messages.GetBotCallbackAnswerRequest) and req.msg_id == 2 and req.data == b"buy"
    assert out == "Order placed"


async def test_press_button_url_and_out_of_range(fake):
    fake.msgs = [mk(2, reply_markup=markup(types.KeyboardButtonUrl(text="Shop", url="https://shop.example")))]
    assert "https://shop.example" in await messages.press_inline_button(chat_id=1, message_id=2, button_index=0)
    assert "out of range" in await messages.press_inline_button(chat_id=1, message_id=2, button_index=3)
    with pytest.raises(RuntimeError, match="button_text or button_index"):
        await messages.press_inline_button(chat_id=1)


# ---------- 下载 / 草稿 ----------


async def test_download_media_default_path_gets_real_extension(fake, tmp_path):
    fake.msgs = [mk(5, media=MessageMediaDocument())]
    out = await messages.download_media(chat_id=1, message_id=5, file_path="ticket.jpg")
    assert out == f"Media downloaded to {(tmp_path / 'ticket.pdf').resolve()}."
    out = await messages.download_media(chat_id=1, message_id=5)
    assert str((tmp_path / "downloads").resolve()) in out
    with pytest.raises(RuntimeError, match="outside allowed roots"):
        await messages.download_media(chat_id=1, message_id=5, file_path="/etc/x")


async def test_save_and_clear_draft(fake):
    assert "saved" in await messages.save_draft(chat_id=1, message="hi", reply_to_msg_id=3)
    req = last(fake, "call")[1][0]
    assert isinstance(req, functions.messages.SaveDraftRequest) and req.reply_to.reply_to_msg_id == 3
    assert "cleared" in await messages.save_draft(chat_id=1, message="")


async def test_get_drafts(fake):
    fake.result = SimpleNamespace(updates=[
        SimpleNamespace(peer=types.PeerChannel(channel_id=123), draft=types.DraftMessage(message="hello", date=DT)),
        SimpleNamespace(peer=types.PeerUser(user_id=1), draft=types.DraftMessageEmpty()),
    ])
    out = await messages.get_drafts()
    assert '"peer_id":-1000000000123' in out and '"count":1' in out
