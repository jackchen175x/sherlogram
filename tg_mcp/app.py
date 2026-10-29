"""配置、Telegram 连接、MCP 实例、常驻进程的壳。

各工具模块只从这里拿 ``mcp`` / ``client`` / ``logger``；本模块不反向依赖任何工具模块。
"""

import asyncio
import logging
import os
import socket
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional

import nest_asyncio
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pythonjsonlogger.json import JsonFormatter
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")
SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME")

# 全电脑只开一份（2026-10-23）：launchd 用 --http 常驻一份，各 Claude 窗口用网址连它。
# 以前每个窗口各起一份 stdio，十来份共用一串登录凭证互相挤下线，其中一份涨到 100G。
HTTP_PORT = int(os.getenv("TG_MCP_HTTP_PORT", "8799"))
HTTP_MODE = False  # main() 按 --http 设置
# 发文件 / 下载文件只许碰这些目录；main() 按命令行位置参数设置（见 common.readable_path）
ALLOWED_ROOTS: List[Path] = []
# 内存上限（MB）。2026-10-23 有一份进程连着断线重连一天半，涨到 100G，
# 把 16G 的 Mac 拖死、交换文件吃掉 30G 硬盘。正常一份 90~150M。
MEMORY_LIMIT_MB = int(os.getenv("TG_MCP_MEMORY_LIMIT_MB", "2048"))
# 启动阶段总超时（秒）。客户端握手 30s 就放弃了，超过这个数还没起来的进程
# 留着只会占用同一个 session string，把后来的进程一起卡死在 auth。
STARTUP_TIMEOUT = int(os.getenv("TG_MCP_STARTUP_TIMEOUT", "45"))
# 工具模块想在登录完成后挂一个后台任务（比如市场雷达的定时器），往这里 append 一个 async 函数；本模块不认识它们
STARTUP_TASKS: List = []

mcp = FastMCP("telegram")
client = TelegramClient(
    StringSession(SESSION_STRING) if SESSION_STRING else SESSION_NAME, API_ID, API_HASH
)

# ---------- 日志 ----------
# 默认 WARNING 而非 ERROR：重连、限速、心跳恢复都是 warning 级，
# 之前它们永远不落盘，"到底被限速过几次"是纯黑箱 —— 而这个日志是唯一故障线索。
logger = logging.getLogger("telegram_mcp")
_log_level = getattr(logging, os.getenv("LOG_LEVEL", "WARNING").upper(), logging.WARNING)
logger.setLevel(_log_level)
_console = logging.StreamHandler()
_console.setLevel(_log_level)
_console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
logger.addHandler(_console)
# 可用 TG_MCP_LOG 改写路径：跑测试时指向别处，否则测试噪音会淹掉真实故障线索
_log_file = os.getenv("TG_MCP_LOG", str(Path(__file__).resolve().parent.parent / "mcp_errors.log"))
try:
    _file = logging.FileHandler(_log_file, mode="a")
    _file.setLevel(logging.WARNING)
    _file.setFormatter(
        JsonFormatter("%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    )
    logger.addHandler(_file)
except Exception as _e:  # 日志文件开不了就只留控制台
    logger.error(f"Failed to set up log file handler: {_e}")


# ---------- 常驻进程 ----------


def _footprint_mb() -> Optional[int]:
    """本进程真实内存占用（含被压缩、换出的部分），只在 macOS 上取得到。"""
    if sys.platform != "darwin":
        return None
    import ctypes

    buf = ctypes.create_string_buffer(256)
    if ctypes.CDLL(None).proc_pid_rusage(os.getpid(), 0, buf) != 0:
        return None
    # rusage_info_v0.ri_phys_footprint 在第 72 字节
    return int.from_bytes(buf.raw[72:80], "little") // 1048576


async def _heartbeat(interval: int = 120) -> None:
    """后台心跳：定期检查连接，断连时自动恢复；内存超限就退出让 launchd 拉起。"""
    while True:
        await asyncio.sleep(interval)
        mb = _footprint_mb()
        if mb and mb > MEMORY_LIMIT_MB:
            logger.error(f"Heartbeat: memory {mb}MB > {MEMORY_LIMIT_MB}MB, exiting")
            os._exit(1)
        try:
            if not client.is_connected():
                logger.warning("Heartbeat: client disconnected, reconnecting...")
                await client.connect()
                logger.info("Heartbeat: reconnected")
        except Exception as e:
            logger.error(f"Heartbeat reconnect failed: {e}")


async def _startup() -> None:
    print("Starting Telegram client...", file=sys.stderr)
    await client.start()
    me = await client.get_me()
    print(f"Logged in as: {me.first_name} (@{me.username or 'N/A'})", file=sys.stderr)
    print("Warming entity cache...", file=sys.stderr)
    await client.get_dialogs()


async def _serve() -> None:
    heartbeat, extras = None, []
    try:
        try:
            await asyncio.wait_for(_startup(), timeout=STARTUP_TIMEOUT)
        except asyncio.TimeoutError:
            print(
                f"Startup timed out after {STARTUP_TIMEOUT}s — exiting so this process "
                "does not hold the shared session. Check for other instances.",
                file=sys.stderr,
            )
            os._exit(1)
        heartbeat = asyncio.create_task(_heartbeat())
        extras = [asyncio.create_task(t()) for t in STARTUP_TASKS]
        print("Telegram client started. Running MCP server...", file=sys.stderr)
        if HTTP_MODE:
            await mcp.run_streamable_http_async()
        else:
            await mcp.run_stdio_async()
    except Exception as e:
        print(f"Error starting client: {e}", file=sys.stderr)
        if "AuthKeyDuplicated" in type(e).__name__ or "two different IP addresses" in str(e):
            # 同一串登录凭证被两个程序同时用，Telegram 服务器直接作废它，只能人工重登一次。
            print(
                "\n========================================\n"
                "Telegram 登录凭证已被服务器作废。\n"
                "原因：同一串凭证在两个地方同时使用。\n"
                "怎么修：在终端里跑一次（会给你手机发验证码）\n"
                "  在项目目录里跑：uv run python relogin.py\n"
                "规矩：这串凭证只给一个程序用，别在别的工具或别的机器上同时配。\n"
                "========================================\n",
                file=sys.stderr,
            )
        if isinstance(e, sqlite3.OperationalError) and "database is locked" in str(e):
            print("Database lock detected. Ensure no other instances are running.", file=sys.stderr)
        sys.exit(1)
    finally:
        for t in [heartbeat, *extras]:
            if t:
                t.cancel()
        try:
            await client.disconnect()
        except Exception:
            pass


def _shared_server_running() -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", HTTP_PORT)) == 0


def _roots_from_argv(argv: List[str]) -> List[Path]:
    roots: List[Path] = []
    for raw in argv:
        root = Path(raw).expanduser()
        if not root.exists():
            raise SystemExit(f"Allowed root does not exist: {root}")
        root = root.resolve(strict=True)
        if root not in roots:
            roots.append(root)
    return roots


def main() -> None:
    """用法：``main.py [--http] [允许发/收文件的目录 ...]``。"""
    global HTTP_MODE, ALLOWED_ROOTS
    argv = sys.argv[1:]
    HTTP_MODE = "--http" in argv
    ALLOWED_ROOTS = _roots_from_argv([a for a in argv if a != "--http"])
    if HTTP_MODE:
        from mcp.server.transport_security import TransportSecuritySettings

        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = HTTP_PORT
        mcp.settings.stateless_http = True
        mcp.settings.log_level = "WARNING"
        logging.getLogger().setLevel(logging.WARNING)  # 常驻进程，daemon.log 别被每次调用刷满
        # 下面两类每天几十条、全是常态：Telegram 服务器定期断开再自动重连；
        # 无状态 HTTP 下请求已答完、SDK 收尾时报 ClosedResourceError 大段堆栈（调用都成功）。
        # ponytail: 直接压掉；升级 mcp 库修好这个 SDK 问题后可删第二行
        logging.getLogger("telethon").setLevel(logging.ERROR)
        logging.getLogger("mcp.server.streamable_http").setLevel(logging.CRITICAL)
        # 挡住网页借浏览器冒充本机来调（DNS rebinding）：只认本机 Host、不认任何网页 Origin
        mcp.settings.transport_security = TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*"], allowed_origins=[]
        )
    elif _shared_server_running():
        sys.exit(
            f"共用的 Telegram 连接已在 127.0.0.1:{HTTP_PORT} 运行，不再另开一份。"
            f"Claude 配置里请用 http://127.0.0.1:{HTTP_PORT}/mcp 连它。"
        )
    nest_asyncio.apply()
    asyncio.run(_serve())
