import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

from tg_mcp import chats


class _FakeClient:
    def __init__(self, target, target_message):
        self.target = target
        self.target_message = target_message

    async def get_participants(self, entity, limit=0):
        assert entity is self.target
        return SimpleNamespace(total=199)

    async def get_dialogs(self):
        wrong_entity = SimpleNamespace(id=999)
        wrong_message = SimpleNamespace(message="private unrelated message")
        return [
            SimpleNamespace(
                entity=wrong_entity,
                unread_count=44,
                message=wrong_message,
            )
        ]

    async def get_messages(self, entity, limit=1):
        assert entity is self.target
        assert limit == 1
        return [self.target_message]


@pytest.mark.asyncio
async def test_get_chat_does_not_treat_offset_dialog_as_target(monkeypatch):
    target = SimpleNamespace(id=123, title="Target Channel", username="target")
    target_message = SimpleNamespace(
        sender=SimpleNamespace(title="Target Channel"),
        date=datetime(2026, 8, 5, 5, 0, tzinfo=timezone.utc),
        message="actual target post",
    )
    fake = _FakeClient(target, target_message)

    async def fake_resolve_entity(chat_id):
        assert chat_id == 123
        return target

    monkeypatch.setattr(chats, "client", fake)
    monkeypatch.setattr(chats, "resolve_entity", fake_resolve_entity)

    result = await chats.get_chat(123)

    assert "actual target post" in result
    assert "private unrelated message" not in result
    assert "Unread Messages: 44" not in result
