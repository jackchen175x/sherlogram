# tg-mcp

[English](README.md) · 中文

让 AI（Claude 或任何支持 MCP 的助手）用你的 Telegram 账号干活：进群、读消息、搜全网公开帖、查人、管群、发消息、整理分组……**39 个工具、98 项功能**，一份连接全电脑共用。

它跟别的 Telegram MCP 不一样的地方：**它是为「顺藤摸瓜找源头」长出来的。**

## 一套找源头的打法

场景：你想知道某种货或服务真正的源头在哪、中间商都是谁。

1. **进群听人说话** —— `join_chat` 进相关群，`list_messages` 读、`search_global` 搜关键词：谁在报价、报什么价、贴了什么网址。
2. **顺着网址找上游** —— 拿到供货网站，看它背后调的接口和域名，找到它的上游；多找几个供货网站，上游会重叠。
3. **一层层往上追** —— 对上游再做一遍，直到追到源头。
4. **回 Telegram 核对** —— `get_chat` 查人（简介、共同群、官方 scam/fake 标记），`search_public_posts` 搜全网公开帖，`list_messages(from_user=…)` 把一个人在群里的全部帖子拉出来：是不是真人、什么时候开始发、转的是不是老帖。

每条消息都带「来路」：转发自哪、原帖多久之前、改过没、是不是机器人代发。把三个月前的老帖改个价冒充现货，在这里一眼看穿。

配套两个脚本把这套做成**可复算**的（同一个卖家问两次得到同一个结论）：

- `market_watch.py` —— 定点复查一批卖家：还在不在、活不活跃、旧帖在不在；扫负面语料；跟上一次快照比对。跨时间比对是全部价值，所以产物都带日期、不覆盖。
- `sockpuppet.py` —— 纯离线马甲聚类：哪些号是同一个人。

## 快速开始

```bash
git clone https://github.com/jackchen175x/tg-mcp && cd tg-mcp
uv sync
cp .env.example .env     # 填 TELEGRAM_API_ID / TELEGRAM_API_HASH（my.telegram.org/apps 申请）
uv run python relogin.py # 手机收验证码登录，凭证写进 .env
```

接到 Claude Code（两种，选一）：

```bash
# 方式一：每个窗口各起一份（简单，但多个窗口同时开会互相挤下线，见下文）
claude mcp add -s user telegram -- uv --directory /path/to/tg-mcp run python main.py ~/Downloads

# 方式二：全电脑只开一份常驻，各窗口用网址连它（推荐）
uv run python main.py --http ~/Downloads ~/Desktop &
claude mcp add --transport http -s user telegram http://127.0.0.1:8799/mcp
```

命令末尾的目录是**允许发文件 / 下载文件的目录**，不给就禁用那几个工具（见「安全线」）。

## 工具（39 个）

一个工具管一类事，用 `action` 参数选具体动作，所以工具清单只有原版三分之一大，功能一项不少。

| 类别 | 工具 |
|---|---|
| 我自己 | `get_me` · `update_profile`（名字/简介/头像）· `privacy`（get/set）· `set_bot_commands` · `get_sticker_sets` |
| 群 / 频道 / 人 | `list_chats` · `get_chat`（简介、共同群、在线状态、头像、scam/fake）· `get_participants`（成员/管理员/被封）· `list_topics` · `join_chat`（用户名/链接/邀请码）· `leave_chat` · `create_chat` · `edit_chat` · `manage_member`（invite/ban/unban/promote/demote）· `get_invite_link` · `get_recent_actions` · `set_chat_state`（mute/unmute/archive/unarchive） |
| 消息 | `list_messages`（最新/搜词/按日期/只看某人/某条前后）· `get_pinned_messages` · `send_message`（文字/回复/文件/语音/贴纸/GIF）· `send_contact` · `edit_message` · `delete_message` · `forward_message` · `pin_message` · `mark_as_read` · `react`（add/remove/list）· `create_poll` · `press_inline_button` · `download_media` · `save_draft` · `get_drafts` |
| 搜索 | `search_global`（自己能看到的所有聊天）· `search_public_chats`（公开群和频道）· `search_public_posts`（全网公开帖，不花星星）· `search_gifs` |
| 联系人 | `list_contacts`（搜/黑名单/导出）· `manage_contact`（add/delete/block/unblock/import） |
| 分组 | `list_folders` · `manage_folder`（create/delete/add_chat/remove_chat/reorder） |

## 一串凭证只给一个程序用

同一串登录凭证同时被两个程序用（两个窗口、Claude 和另一个工具、两台电脑），Telegram 会直接作废它，所有地方一起掉线，只能重新收验证码登录。所以：

- 推荐常驻一份（`--http`），各窗口连网址。常驻那份在跑时，再用 stdio 方式起 `main.py` 会直接拒绝。
- macOS 开机自启的写法见 `examples/launchd.plist`；内存超 2G（`TG_MCP_MEMORY_LIMIT_MB`）自己退出让 launchd 拉起。
- 别的机器或别的工具要用 Telegram：另注册账号、另拿一串凭证。
- 凭证被作废后报 `AuthKeyDuplicated`：跑 `uv run python relogin.py`（旧凭证备份成 `.env.bak`）。

## 安全线

- **发文件 / 下载 / 换头像只许碰启动时给的目录**，没给就禁用这几个工具；路径里的 `..`、通配符一律拒绝；语音只收 .ogg/.opus，贴纸只收 .webp，头像只收图片，各有大小上限。stdio 模式会优先问客户端的 MCP Roots。
- 常驻模式只听 `127.0.0.1`，只认本机 Host、不认任何网页 Origin，网页借浏览器冒充本机调不到。
- 出错**以异常抛出**而不是返回一段字，MCP 协议层才会标 isError —— 否则 AI 在批量调查里会把「连接断了」当成「这个卖家没负面记录」略过。

## 排障

- 连不上：看 `mcp_errors.log`（JSON 格式，只记 warning 以上）；`LOG_LEVEL=INFO` 看更多
- 限速（FloodWait）：等提示的秒数；用数字 ID 比 @用户名 少触发限速（用户名解析每次都查服务器）
- 启动慢：首次要拉全部会话预热缓存，10~30 秒；超过 `TG_MCP_STARTUP_TIMEOUT`（默认 45 秒）自动退出，免得占着凭证
- 测试：`TG_MCP_LOG=/dev/null uv run pytest -q`

## 许可

Apache-2.0。改自 [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp)。
