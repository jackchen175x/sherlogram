# tg-mcp

English · [中文](README.zh-CN.md)

Let an AI (Claude, or any MCP-capable assistant) work inside your Telegram account: join groups, read messages, search every public post on Telegram, look people up, run groups, send messages, organize folders — **47 tools, 105 features**, one connection shared by every window on your machine.

What makes it different from other Telegram MCP servers: **it grew out of tracing supply chains back to their source** — one sentence draws the chain, one sentence grades a seller.

## One sentence, one supply chain

Tell Claude "read the posts with links in these five groups, follow two hops, where does it come from?" — it calls `trace_source` once and 50 seconds later hands you this graph (a real run; unrelated branches removed, names masked):

```mermaid
graph LR
  classDef seller fill:#f4f4f4,stroke:#999
  classDef site fill:#e8f1ff,stroke:#3b82f6
  classDef source fill:#ffedd5,stroke:#ea580c,stroke-width:3px
  s1["@seller_A"]:::seller
  s2["@seller_B"]:::seller
  b1["@group_helper_bot"]
  w1["uu-**.net<br/>AI video API"]:::source
  w2["uu-**.com<br/>video CMS vendor"]:::source
  u1["@u***_com<br/>upstream U"]:::source
  g1["@u*_cms<br/>U's group"]:::source
  c1["@u*_cms_com<br/>U's channel"]
  s1 -->|links to| w1
  s1 -->|links to| w2
  s1 -->|mentions| u1
  s2 -->|links to| w1
  s2 -->|links to| w2
  s2 -->|mentions| u1
  s2 -->|mentions| g1
  u1 -->|bio says| w1
  u1 -->|bio says| w2
  c1 -->|links to| w1
  c1 -->|mentions| u1
  b1 -->|posts in| g1
  b1 -->|links to| w1
  p1["@wholesale_channel"]:::seller
  k1["a***.pro<br/>card shop"]:::site
  kf["@a***_kefu<br/>support"]
  kg["@a***pro<br/>group"]:::source
  p1 -->|links to| k1
  k1 -->|contact on site| kf
  k1 -->|contact on site| kg
  kf -->|bio says| k1
  kf -->|mentions| p1
  kf -->|mentions| kg
  s3["@spam_bot"]:::seller
  t1["t***ai.com"]:::site
  s3 -->|links to| t1
```

> Read 39 posts from 10 sellers, probed 12 sites, re-checked 8 handles. Convergence point: @u***_com (mentioned by 5 accounts).

Read it at a glance: two "sellers" push two sites, and the sites, the bios and the group all point at one upstream U — the sellers are middlemen. The other chain: a channel → its card shop → the support handle the shop lists → whose bio points back at the channel. A closed loop: that one sells its own stock. Orange thick border = where the clues converge; red = flagged as phishing by Cloudflare or flagged by Telegram.

Then say "is @someseller legit?" — it calls `check_seller` once and ten seconds later you get a card: tier A–D, every red flag with the quote it came from:

```json
{"handle":"@v***bot","tier":"C","tier_definition":"证据不足，或长期沉寂，或商业模式风险高  (thin evidence / dormant / risky business model)",
 "red_flags":["活跃度靠刷屏：50 条只有 1 条不同文案（50.0 倍） (activity is spam: 50 posts, 1 distinct text, 50x)"],
 "activity":{"posts_sampled":50,"days_since_last":0,"distinct_texts":1,"spam_ratio":50.0,"posts_with_price":50},
 "provenance":{"edited_old":0,"forwarded_old":0,"via_bot":0},
 "clues":{"domains":["t***ai.com"],"prices":["5 元","98 元","100刀"]},
 "sites":{"t***ai.com":{"verdict":"reachable"}},
 "accusations":[],"related_accounts":[],"confidence":"medium"}
```

Then ask "where does the money from this payment address go?" — same `trace_source`, pass `address`, and it follows the USDT (Tron) transfers two hops down, 14 seconds:

```json
{"summary":"查了 4 个钱包的链上流水。线索汇聚点：TQGB…Zwj2（3 个地址给它打过钱；1 个钱包的钱转到它（资金汇聚）；链上进 2288.0万U、出 1265.0万U）",
 "wallets":{"TNG3…CkVH":{"in_usdt":719414,"out_usdt":6755927,"payers":155,"payees":24,"first":"2026-08-10","last":"2026-09-09",
   "top_out":[{"address":"TQGB…Zwj2","usdt":2879940,"count":2},{"address":"TW3o…oHFp","usdt":1888888,"count":1}],
   "note":"只看了最近 200 笔","explorer":"https://tronscan.org/#/address/TNG3…CkVH"}}}
```

Payment addresses found in posts enter the graph automatically: **two sellers collecting on the same address are one person** — harder evidence than any text clue; whoever the money finally pools at is the upstream.

A storefront domain can be dug into directly: `trace_source(site="…", deep_sites=1)` fetches its homepage for contacts and APIs, then asks three free public sources what sits behind it — **other domains on the same certificate** (one owner, one cert), **other sites on the same server** (looked up by domain on the far side, so a local proxy doesn't matter), and **registrar / registration date / registrant / nameservers**; sibling sites enter the graph with "same cert", "same server" and "same registrant" edges. A certificate older than the current registration gets flagged "domain changed hands, old reputation doesn't count"; `check_seller(deep_sites=True)` raises a red flag when a storefront domain was registered less than a month ago.

Don't keep what you found to yourself: `scam_list` is a **shareable scam list**. `add` runs the seller card on a handle and stores tier, red flags, quoted evidence from the groups, payment addresses and storefronts plus your own one-line note as one record; `query` looks up a handle, numeric id, wallet or domain; `merge` folds in list files other people send you; `export` writes the Rose group-bot federation-ban format so admins can import and ban in one go. The rules copy what works in the wild: **numeric ids only** (usernames are aliases, renaming doesn't help), **no evidence, no entry** (quotes carry chat and date, your own words carry the reporter), merges only add, the worst tier wins. Once a record exists, `check_seller` consults the list automatically: a listed account, or one sharing a payment address with a listed account, goes straight to tier D.

Checking once is not enough: `market_radar` is a **market radar**. `add` puts a seller (handle) or a trace (keywords / a few groups) on the watch list and takes the first snapshot; `run` re-checks only what is on the list (no site-wide re-crawl: tens of seconds, no rate-limit trouble) and diffs it against the last snapshot — gone, tier changed, renamed, officially flagged, new accusations, storefront or price changes, money re-routed, new sellers on a trace, convergence point moved — one line per change, **pushed only when something changed**. Delivery goes through `RADAR_NOTIFY_CMD` (a shell command that gets the text on stdin: Feishu, Slack, any notifier); with `RADAR_EVERY_HOURS=24` the daemon runs it on its own once a day, timed from the last run recorded in the file so restarts don't reset it. Snapshots are dated and appended, never overwritten; `history` shows any watch's full timeline.

Who is the source in time: `who_first` takes an ad text, a storefront domain, an @handle or a wallet, searches by clues and text fragments (your groups + all public posts), groups the hits by **text fingerprint** (zero-width characters, interpuncts and other search-dodging tricks are stripped before comparing), and sorts each group by time: who posted it first, where and when, then which accounts followed and from which day. **Forwards count by their original date and author**, so the origin channel is named even when it was not searched; the opening lines alone are enough to recognise the same post. Each clue also gets its first-appearance date. The seller card gains a `first_seen` field — their first post in the groups or the earliest mention of them, whichever is older — so a "source" that appeared last week and a shop that has been around for two years are told apart at a glance. Limits: Telegram search is word-based and weak on long Chinese sentences, so long copy is split into short fragments; results come newest first and each term scans `limit` messages, and the result says when it did not reach the end.

Size up a new group first: `group_profile` reads its recent few hundred posts and says in one line **what the group is for** (marketplace / discussion / bot-flooded / one-way channel), how many members, when it started, posts per day, how much the top three posters account for, the **ad share** (a price, a storefront link, or sales talk plus a contact counts as an ad), how many **bots** post how much (official bot flag only, no guessing), which **suspected same-person pairs** there are (shared payment address > same storefront > same name stem > identical copy), how many **accusation posts** and who gets named most, plus the main storefronts, typical prices and payment addresses. The top ten posters come as a table: posts, ads among them, distinct texts, last post, storefronts, @mentions, wallets. Everything is computed from the messages themselves — no per-username lookups, no rate-limit exposure; join/leave notices and media without text are not posts. A 14k-member group profiles in 8 seconds, and the first run caught five western-named accounts posting the same copy at each other.

Whether a quote is dear, or suspiciously cheap, needs a market to compare with: `price_board` reads the recent few hundred posts of the groups where the market trades, pairs each price with the product named **before** it (GPT Plus, Claude 20x, GPT Pro 5x, ...; price lists always read "product price", and nearest-match pairing would hand 278 to the Claude 20x that follows; a "7 days", "yearly", "seat" or "shared" between product and price makes it a separate product, so a weekly price is never judged against monthly rates), and gives per product the **median, spread and a too-cheap floor** (median minus three absolute deviations, clamped between 50% and 80% of the median — medians, not means: this market is long-tailed and a few absurd asks would drag a mean anywhere), the 7-day trend, the three cheapest sellers, the **quotes below the floor** (too good to be true, usually fakes or phishing) and the **active sellers who never quote** ("see the storefront", "DM me" is not missing data, it is a point against them). One seller spamming the same price list fifty times counts once, so spammers don't set the median. Once the board is saved, `check_seller` adds `prices_vs_market` to the card: each quote graded cheap / fair / dear against the market, and **a quote below the floor raises a red flag and drops the seller to tier C**. Five groups, 582 posts, 14 seconds; 174 GPT Plus quotes, median 99 yuan.

To share it, add `report=True`: the same call also writes an HTML file into your download directory — an **interactive graph you can drag, zoom, filter by type, save as PNG, mask names on, and export as GEXF for Gephi**, followed by the convergence points, sellers, sites and wallets tables; `check_seller` with `report=True` writes a card page with the quoted evidence. Without MCP, save the tool's JSON to a file and run `python -m tg_mcp.report trace.json` for the same page.


**Limits**: hops, sites probed, handles re-checked, wallets queried and seconds are all parameters; when a budget runs out, `truncated` says where it stopped. Username lookups are rate-limited by Telegram, so by default only 8 handles are re-checked, one second apart. Sites are probed by fetching the homepage only, private-network hosts are refused, page bodies are never returned. Behind a transparent proxy (fake-ip) the real IP is unknown, so "same server" cannot be judged. Site digging uses crt.sh, HackerTarget (free, 20/day) and rdap.org; sites behind Cloudflare show no same-server neighbours and registrants are usually redacted — that is the ceiling of free sources, not a missing lookup. On-chain lookups cover USDT on Tron only (where this market collects payment), through Tron's official free API: works without a key, rate-limited when busy, `TRONGRID_API_KEY` raises the limit; the free API gives no address labels (exchange / risk), so each result links to the explorer. Tiers are relative verifiability, not a purchase endorsement.

## A playbook for finding the source

`trace_source` automates the four steps below. To go step by step by hand (or to keep going where it lost the trail):

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

## Tools (47)

One tool per kind of thing, with an `action` parameter choosing what to do — so the tool list is a third the size of the original 98-tool version with nothing missing.

| Area | Tools |
|---|---|
| Investigation | `trace_source` (one-shot trace: posts → @handles/domains/payment addresses → probe sites for upstream → re-check on Telegram → graph; `address` follows USDT money flows, `site` digs behind a storefront: same cert / same server / registration; `report=True` writes an interactive graph page) · `check_seller` (one-glance trust card, tier A–D with quoted evidence, consults the scam list) · `scam_list` (shared scam list: add / query / merge / export / stats, Rose federation-ban export) · `market_radar` (market radar: add / run / list / history / remove — watch sellers and traces, diff against the last snapshot, push one line per change)· `who_first` (who said it first: an ad text / a storefront / an @handle — who posted it first and where, who copied it since)· `group_profile` (group profile: what it is for, top posters, ad share, bots, suspected same-person pairs, who gets accused)· `price_board` (price board: per-product median, too-cheap floor, trend, cheapest sellers, too-good-to-be-true offers, sellers who never quote; the seller card grades quotes against it) |
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
