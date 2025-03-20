import os
from dotenv import load_dotenv
import asyncio
import nest_asyncio
from mcp.server.fastmcp import FastMCP
from telethon import TelegramClient

load_dotenv()

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME")


mcp = FastMCP("telegram")
client = TelegramClient(TELEGRAM_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)


@mcp.tool()
async def get_chats(page: int, page_size: int = 20) -> str:
    """
    Get a paginated list of chats.

    Args:
        page: Page number (1-indexed).
        page_size: Number of chats per page.
    """
    dialogs = await client.get_dialogs()
    start = (page - 1) * page_size
    end = start + page_size
    if start >= len(dialogs):
        return "Page out of range."
    chats = dialogs[start:end]
    lines = []
    for dialog in chats:
        # For groups or channels, use the title; for users, show first name.
        entity = dialog.entity
        chat_id = entity.id
        title = getattr(entity, "title", None) or getattr(entity, "first_name", "Unknown")
        lines.append(f"Chat ID: {chat_id}, Title: {title}")
    return "\n".join(lines)


@mcp.tool()
async def get_messages(chat_id: int, page: int, page_size: int = 20) -> str:
    """
    Get paginated messages from a specific chat.

    Args:
        chat_id: The ID of the chat.
        page: Page number (1-indexed).
        page_size: Number of messages per page.
    """
    try:
        entity = await client.get_entity(chat_id)
    except Exception as e:
        return f"Could not resolve chat with ID {chat_id}: {e}"

    offset = (page - 1) * page_size
    messages = await client.get_messages(entity, limit=page_size, add_offset=offset)
    if not messages:
        return "No messages found for this page."
    lines = []
    for msg in messages:
        lines.append(f"ID: {msg.id} | Date: {msg.date} | Message: {msg.message}")
    return "\n".join(lines)


@mcp.tool()
async def send_message(chat_id: int, message: str) -> str:
    """
    Send a message to a specific chat.

    Args:
        chat_id: The ID of the chat.
        message: The message content to send.
    """
    try:
        entity = await client.get_entity(chat_id)
    except Exception as e:
        return f"Could not resolve chat with ID {chat_id}: {e}"

    try:
        await client.send_message(entity, message)
        return "Message sent successfully."
    except Exception as e:
        return f"Failed to send message: {e}"


if __name__ == "__main__":
    nest_asyncio.apply()

    async def main() -> None:
        # Start the Telethon client.
        await client.start()
        print("Telegram client started. Running MCP server...")
        # Use the asynchronous entrypoint instead of mcp.run()
        await mcp.run_stdio_async()

    asyncio.run(main())
