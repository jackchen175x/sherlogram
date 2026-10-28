"""所有测试共用的保险：不碰真 Telegram 连接。

.env 里是真登录凭证，而同一串凭证被两个程序同时用会被 Telegram 作废（常驻那份正在用）。
各模块测试都 monkeypatch 自己模块里的 client / resolve_entity；万一哪个用例漏了、
间接走到 common 里的真连接，这里让它当场报错，而不是悄悄去连网。
"""
import os

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")
os.environ.setdefault("TG_MCP_LOG", "/dev/null")

import pytest  # noqa: E402

import tg_mcp.app  # noqa: E402
import tg_mcp.common  # noqa: E402


class _NoNetwork:
    def __getattr__(self, name):
        raise AssertionError(f"测试碰到了真连接 client.{name}：该用例忘了 monkeypatch 它所在模块的 client / resolve_entity")


@pytest.fixture(autouse=True)
def _block_real_client(monkeypatch):
    monkeypatch.setattr(tg_mcp.app, "client", _NoNetwork())
    monkeypatch.setattr(tg_mcp.common, "client", _NoNetwork())
