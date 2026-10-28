# tg-mcp

让 Claude 用 Jack 的 Telegram 账号查群、读消息、搜帖、存草稿。
改自 [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp)（Apache-2.0，见 LICENSE）。

## 工具（17 个）

2026-10-28 从原版 98 个砍到 17 个：按历史会话的真实调用统计，只留用过的，重复的合并。

| 干什么 | 工具 |
|---|---|
| 看自己 / 自己的会话 | `get_me` `list_chats` |
| 查一个群、频道、人、机器人的详情（含简介、共同群数、scam/fake、不透明度） | `get_chat` |
| 群成员 | `get_participants` |
| 读一个群的消息：最新 N 条 / 搜关键词 / 按日期 / 只看某人 | `list_messages` |
| 置顶消息 | `get_pinned_messages` |
| 搜：自己能看到的所有聊天 / 公开群和频道 / 全网公开帖（不花星星） | `search_global` `search_public_chats` `search_public_posts` |
| 进出群 | `subscribe_public_channel` `join_chat_by_link` `leave_chat` |
| 分组 | `list_folders` `add_chat_to_folder` |
| 发消息 / 存草稿（传空字符串＝清草稿）/ 看所有草稿 | `send_message` `save_draft` `get_drafts` |

合并掉的：`get_messages` `get_history` `search_messages` → `list_messages`；`get_user_full_info` `resolve_username` → `get_chat`；`clear_draft` → `save_draft("")`；`check_public_post_search_quota` → `search_public_posts` 每次结果都带剩余额度。
砍掉的工具、发文件那套路径白名单、Docker 都在 git 提交 `fac0664` 里，要用再捡回来。

## 本机用法：全电脑只开一份

同一串登录凭证同时被两个程序用，Telegram 会直接作废它。所以：

- 常驻一份：`~/Library/LaunchAgents/com.jackchen.tg-mcp.plist`，开机自启、挂了自动重启，跑 `main.py --http`，只听 `127.0.0.1:8799`（`TG_MCP_HTTP_PORT` 可改），日志 `daemon.log`
- Claude 连它：`claude mcp add --transport http -s user telegram-mcp http://127.0.0.1:8799/mcp`
- 常驻那份在跑时，stdio 方式起 `main.py` 会直接拒绝
- 内存超 2G（`TG_MCP_MEMORY_LIMIT_MB`）自己退出，launchd 拉起
- 重启：`launchctl kickstart -k gui/$(id -u)/com.jackchen.tg-mcp`
- **凭证只给这一份用**。Codex、Mac mini 要用 Telegram，另注册账号、另拿凭证

## 附带脚本

- `market_watch.py` `sockpuppet.py`：卖家复查，用法见 skill `tg-seller-check`。`market_watch.py` 会临时自己连一次，跑完就断
- `relogin.py`：凭证被作废后重登并写回 `.env`（旧的备份成 `.env.bak`）。要收手机验证码，只能 Jack 本人跑：
  `cd ~/tg-mcp && uv run python relogin.py`

## 排障

- 连不上：先看 `launchctl print gui/$(id -u)/com.jackchen.tg-mcp` 和 `daemon.log`
- 报 `AuthKeyDuplicated`：凭证被两处同时用、已作废，跑 `relogin.py`
- 限速（FloodWait）：等提示的秒数再试
- 启动慢：首次要拉全部会话预热缓存，10~30 秒
- 详细日志：环境变量 `LOG_LEVEL=INFO`；出错记录在 `mcp_errors.log`
- 测试：`TG_MCP_LOG=/dev/null .venv/bin/python -m pytest -q`
