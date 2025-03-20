![](https://badge.mcpx.dev 'MCP')
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green?style=flat-square)](https://opensource.org/licenses/Apache-2.0)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-blue)](https://www.linkedin.com/in/eugene-evstafev-716669181/)

# Telegram MCP Server

A Telegram MCP (Model Context Protocol) server built using Python, Telethon, and MCP Python SDK. This MCP server provides simple tools for interacting with Telegram chats directly through MCP-compatible hosts, such as Claude for Desktop.

## Tools Provided

- **`get_chats`**: Retrieve a paginated list of your Telegram chats.
- **`get_messages`**: Retrieve paginated messages from a specific chat.
- **`send_message`**: Send a message to a specific chat.

## Requirements

- Python 3.10 or higher
- [Telethon](https://docs.telethon.dev/) package
- [MCP Python SDK](https://modelcontextprotocol.io/docs/)
- [UV](https://astral.sh/uv/) (optional but recommended)

## Installation and Setup

### Clone the Repository

```bash
git clone https://github.com/chigwell/telegram-mcp
cd telegram-mcp
```

### Create Environment File

Copy and rename `.env.example` to `.env` and fill it with your Telegram credentials obtained from [https://my.telegram.org/apps](https://my.telegram.org/apps):

```bash
cp .env.example .env
```

Your `.env` file should look like:

```env
TELEGRAM_API_ID=your_api_id_here
TELEGRAM_API_HASH=your_api_hash_here
TELEGRAM_SESSION_NAME=your_session_name
```

### Setup Python Environment

Use `uv` to set up the Python environment and install dependencies:

```bash
uv venv
source .venv/bin/activate
uv add "mcp[cli]" telethon python-dotenv nest_asyncio
```

### Run the Server (First-time Auth)

The first time you run the server, Telethon will prompt you to enter a Telegram authentication code:

```bash
uv run main.py
```

Authenticate by entering the code sent to your Telegram client. This step is only required once.

## Integrating with Claude for Desktop

### macOS/Linux

Edit your Claude Desktop configuration:

```bash
nano ~/Library/Application\ Support/Claude/claude_desktop_config.json
```

Add this MCP server configuration:

```json
{
    "mcpServers": {
        "telegram-mcp": {
            "command": "uv",
            "args": [
                "--directory",
                "/ABSOLUTE_PATH/telegram-mcp",
                "run",
                "main.py"
            ]
        }
    }
}
```

Ensure you replace `/ABSOLUTE_PATH/telegram-mcp` with your project's absolute path.

### Windows

Edit your Claude Desktop configuration:

```powershell
nano $env:AppData\Claude\claude_desktop_config.json
```

Add this MCP server configuration:

```json
{
    "mcpServers": {
        "telegram-mcp": {
            "command": "uv",
            "args": [
                "--directory",
                "C:\\ABSOLUTE_PATH\\telegram-mcp",
                "run",
                "main.py"
            ]
        }
    }
}
```

Ensure you replace `C:\ABSOLUTE_PATH\telegram-mcp` with your project's absolute path.

## Usage

Once integrated, your Telegram tools (`get_chats`, `get_messages`, and `send_message`) will become available within the Claude for Desktop UI or any other MCP-compatible client.

## License

This project is licensed under the [Apache 2.0 License](https://opensource.org/licenses/Apache-2.0).