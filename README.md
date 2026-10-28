# tg-mcp

English · [中文](README.zh-CN.md)

Let an AI (Claude, or any MCP-capable assistant) work inside your Telegram account: join groups, read messages, search every public post on Telegram, look people up, run groups, send messages, organize folders — **39 tools, 98 features**, one connection shared by every window on your machine.

What makes it different from other Telegram MCP servers: **it grew out of tracing supply chains back to their source.**

## A playbook for finding the source

Scenario: you want to know where some product or service really comes from, and who the middlemen are.

1. **Join the groups and listen** — `join_chat` the relevant groups, `list_messages` to read, `search_global` for keywords: who is quoting prices, which prices, which URLs.
2. **Follow the URLs upstream** — take the storefront sites, look at the APIs and domains behind them, find their suppliers; collect several storefronts and the suppliers start to overlap.
3. **Climb level by level** — repeat for each supplier until you hit the source.
4. **Cross-check back on Telegram** — `get_chat` to profile a person (bio, shared groups, official scam/fake flags), `search_public_posts` across all public Telegram, `list_messages(from_user=…)` to pull one person's entire posting history in a group: real human or not, when they started, are they forwarding stale ads.

Every message carries its **provenance**: forwarded from where, how old the original is, whether it was edited, whether a bot posted it. Someone editing a three-month-old post to a new price and passing it off as fresh stock is obvious at a glance.

Two companion scripts make this **reproducible** (ask about the same seller twice, get the same verdict):

- `market_watch.py` — re-check a list of sellers: still around, still active, old posts still up; scan negative-sentiment corpora; diff against the previous snapshot. Comparing across time is the whole point, so every output is dated and never overwritten.
- `sockpuppet.py` — offline sockpuppet clustering: which accounts are the same person.

## Quick start

```bash
git clone https://github.com/jackchen175x/tg-mcp && cd tg-mcp
uv sync
cp .env.example .env     # fill TELEGRAM_API_ID / TELEGRAM_API_HASH from my.telegram.org/apps
uv run python relogin.py # log in with the code sent to your phone; the session string is written to .env
```

Connect to Claude Code (pick one):

```bash
# Option 1: one process per window (simple, but several windows at once will kick each other out — see below)
claude mcp add -s user telegram -- uv --directory /path/to/tg-mcp run python main.py ~/Downloads

# Option 2: one shared daemon for the whole machine, every window connects by URL (recommended)
uv run python main.py --http ~/Downloads ~/Desktop &
claude mcp add --transport http -s user telegram http://127.0.0.1:8799/mcp
```

The directories at the end of the command are the **only places file tools may read from or write to**; omit them and those tools are disabled (see "Safety").

## Tools (39)

One tool per kind of thing, with an `action` parameter choosing what to do — so the tool list is a third the size of the original 98-tool version with nothing missing.

| Area | Tools |
|---|---|
| Me | `get_me` · `update_profile` (name/bio/photo) · `privacy` (get/set) · `set_bot_commands` · `get_sticker_sets` |
| Chats, channels, people | `list_chats` · `get_chat` (bio, shared groups, online status, photos, scam/fake) · `get_participants` (members/admins/banned) · `list_topics` · `join_chat` (username/link/invite hash) · `leave_chat` · `create_chat` · `edit_chat` · `manage_member` (invite/ban/unban/promote/demote) · `get_invite_link` · `get_recent_actions` · `set_chat_state` (mute/unmute/archive/unarchive) |
| Messages | `list_messages` (latest/keyword/date range/one sender/around a message) · `get_pinned_messages` · `send_message` (text/reply/file/voice/sticker/GIF) · `send_contact` · `edit_message` · `delete_message` · `forward_message` · `pin_message` · `mark_as_read` · `react` (add/remove/list) · `create_poll` · `press_inline_button` · `download_media` · `save_draft` · `get_drafts` |
| Search | `search_global` (everything you can see) · `search_public_chats` (public groups and channels) · `search_public_posts` (all public Telegram, no Stars spent) · `search_gifs` |
| Contacts | `list_contacts` (search/blocked/export) · `manage_contact` (add/delete/block/unblock/import) |
| Folders | `list_folders` · `manage_folder` (create/delete/add_chat/remove_chat/reorder) |

## One session string, one process

If the same session string is used by two processes at once (two windows, Claude plus another tool, two machines), Telegram revokes it: everything disconnects and you have to log in again with a phone code. So:

- Run one daemon (`--http`) and point every window at the URL. While the daemon is running, starting `main.py` in stdio mode refuses to start.
- macOS launch-at-login example: `examples/launchd.plist`; the process exits on its own above 2 GB RSS (`TG_MCP_MEMORY_LIMIT_MB`) and launchd restarts it.
- Another machine or another tool that needs Telegram: register another account and get its own session string.
- After a revocation you'll see `AuthKeyDuplicated`: run `uv run python relogin.py` (the old string is backed up to `.env.bak`).

## Safety

- **Sending files, downloading and changing photos may only touch the directories given at startup**; with none given those tools are disabled. `..` and shell wildcards in paths are rejected; voice notes must be .ogg/.opus, stickers .webp, photos image types, each with a size cap. In stdio mode the client's MCP Roots take precedence.
- The daemon binds `127.0.0.1` only, accepts only local `Host` headers and no web `Origin`, so a web page cannot reach it through your browser (DNS rebinding).
- Errors are **raised**, not returned as strings, so the MCP layer sets `isError` — otherwise an AI running a batch investigation would read "connection lost" as "this seller has no negative history".

## Troubleshooting

- Cannot connect: check `mcp_errors.log` (JSON, warnings and above); `LOG_LEVEL=INFO` for more
- Rate limited (FloodWait): wait the number of seconds shown; numeric IDs trigger fewer limits than @usernames (username resolution hits the server every time)
- Slow start: the first run pulls all dialogs to warm the cache, 10–30 s; past `TG_MCP_STARTUP_TIMEOUT` (default 45 s) it exits rather than hold the session
- Tests: `TG_MCP_LOG=/dev/null uv run pytest -q`

## License

Apache-2.0. Forked from [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp).
