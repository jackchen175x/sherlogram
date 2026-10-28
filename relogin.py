"""重新登录 Telegram，并把新凭证写回 .env。

为什么要重登：同一串凭证在两个地方同时用，Telegram 服务器会直接作废它。
用法：cd ~/tg-mcp && uv run --with telethon python relogin.py
"""
import asyncio, os, re, shutil, sys
from telethon import TelegramClient
from telethon.sessions import StringSession

ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

def read_env():
    d = {}
    for line in open(ENV, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d

def write_session(new_value):
    shutil.copy(ENV, ENV + ".bak")           # 先备份旧的
    src = open(ENV, encoding="utf-8").read()
    if re.search(r"^TELEGRAM_SESSION_STRING=", src, flags=re.M):
        src = re.sub(r"^TELEGRAM_SESSION_STRING=.*$", "TELEGRAM_SESSION_STRING=" + new_value, src, flags=re.M)
    else:
        src = src.rstrip("\n") + "\nTELEGRAM_SESSION_STRING=" + new_value + "\n"
    open(ENV, "w", encoding="utf-8").write(src)
    os.chmod(ENV, 0o600)

async def main():
    e = read_env()
    print("\n要用你的 Telegram 账号重新登录一次。手机上会收到验证码。\n")
    c = TelegramClient(StringSession(), int(e["TELEGRAM_API_ID"]), e["TELEGRAM_API_HASH"])
    await c.start()                           # 会依次问手机号、验证码、两步验证密码
    me = await c.get_me()
    write_session(c.session.save())
    await c.disconnect()
    print(f"\n✅ 登录成功：{me.first_name or ''} @{me.username or '无用户名'}")
    print("新凭证已写回 .env（旧的备份成 .env.bak）。")
    print("接下来在 Claude 里说一声，我来重启工具并建分组。\n")

asyncio.run(main())
