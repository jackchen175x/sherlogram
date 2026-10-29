"""入口：把所有工具模块导入一遍（各自在导入时用 @mcp.tool 注册），然后启动。"""

from tg_mcp import chats, contacts, first, folders, me, messages, radar, search, trace  # noqa: F401
from tg_mcp.app import main

if __name__ == "__main__":
    main()
