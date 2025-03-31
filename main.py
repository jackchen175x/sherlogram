import os
import sys
import time
from dotenv import load_dotenv
import asyncio
import nest_asyncio
from mcp.server.fastmcp import FastMCP
from telethon import TelegramClient
from telethon.sessions import StringSession
import sqlite3
from telethon import utils
from telethon.tl.types import User, Chat, Channel
from telethon.tl.functions.contacts import SearchRequest
from datetime import datetime, timedelta
import json
from typing import List, Dict, Optional, Union, Any

load_dotenv()

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME")

# Check if a string session exists in environment, otherwise use file-based session
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")

mcp = FastMCP("telegram")

if SESSION_STRING:
    # Use the string session if available
    client = TelegramClient(StringSession(SESSION_STRING), TELEGRAM_API_ID, TELEGRAM_API_HASH)
else:
    # Use file-based session
    client = TelegramClient(TELEGRAM_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)


def format_entity(entity) -> Dict[str, Any]:
    """Helper function to format entity information consistently."""
    result = {"id": entity.id}
    
    if hasattr(entity, "title"):
        result["name"] = entity.title
        result["type"] = "group" if isinstance(entity, Chat) else "channel"
    elif hasattr(entity, "first_name"):
        name_parts = []
        if entity.first_name:
            name_parts.append(entity.first_name)
        if hasattr(entity, "last_name") and entity.last_name:
            name_parts.append(entity.last_name)
        result["name"] = " ".join(name_parts)
        result["type"] = "user"
        if hasattr(entity, "username") and entity.username:
            result["username"] = entity.username
        if hasattr(entity, "phone") and entity.phone:
            result["phone"] = entity.phone
    
    return result


def format_message(message) -> Dict[str, Any]:
    """Helper function to format message information consistently."""
    result = {
        "id": message.id,
        "date": message.date.isoformat(),
        "text": message.message or "",
    }
    
    if message.from_id:
        result["from_id"] = utils.get_peer_id(message.from_id)
    
    if message.media:
        result["has_media"] = True
        result["media_type"] = type(message.media).__name__
    
    return result


@mcp.tool()
async def get_chats(page: int = 1, page_size: int = 20) -> str:
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
async def get_messages(chat_id: int, page: int = 1, page_size: int = 20) -> str:
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


@mcp.tool()
async def search_contacts(query: str) -> str:
    """
    Search for contacts by name or phone number.
    
    Args:
        query: The search term to look for in contact names or phone numbers.
    """
    try:
        # Search in your contacts
        contacts = await client.get_contacts()
        results = []
        
        for contact in contacts:
            if not contact:
                continue
                
            name = f"{getattr(contact, 'first_name', '')} {getattr(contact, 'last_name', '')}".strip()
            username = getattr(contact, 'username', '')
            phone = getattr(contact, 'phone', '')
            
            if (query.lower() in name.lower() or 
                (username and query.lower() in username.lower()) or 
                (phone and query in phone)):
                
                contact_info = f"ID: {contact.id}, Name: {name}"
                if username:
                    contact_info += f", Username: @{username}"
                if phone:
                    contact_info += f", Phone: {phone}"
                
                results.append(contact_info)
        
        if not results:
            return f"No contacts found matching '{query}'."
            
        return "\n".join(results)
    except Exception as e:
        return f"Error searching contacts: {e}"


@mcp.tool()
async def list_messages(chat_id: int, limit: int = 20, search_query: str = None, 
                        from_date: str = None, to_date: str = None) -> str:
    """
    Retrieve messages with optional filters.
    
    Args:
        chat_id: The ID of the chat to get messages from.
        limit: Maximum number of messages to retrieve.
        search_query: Filter messages containing this text.
        from_date: Filter messages starting from this date (format: YYYY-MM-DD).
        to_date: Filter messages until this date (format: YYYY-MM-DD).
    """
    try:
        entity = await client.get_entity(chat_id)
        
        # Parse date filters if provided
        from_date_obj = None
        to_date_obj = None
        
        if from_date:
            try:
                from_date_obj = datetime.strptime(from_date, "%Y-%m-%d")
            except ValueError:
                return f"Invalid from_date format. Use YYYY-MM-DD."
                
        if to_date:
            try:
                to_date_obj = datetime.strptime(to_date, "%Y-%m-%d")
                # Set to end of day
                to_date_obj = to_date_obj + timedelta(days=1, microseconds=-1)
            except ValueError:
                return f"Invalid to_date format. Use YYYY-MM-DD."
        
        # Prepare filter parameters
        params = {}
        if search_query:
            params['search'] = search_query
        
        messages = await client.get_messages(entity, limit=limit, **params)
        
        # Apply date filters (Telethon doesn't support date filtering in get_messages directly)
        if from_date_obj or to_date_obj:
            filtered_messages = []
            for msg in messages:
                if from_date_obj and msg.date < from_date_obj:
                    continue
                if to_date_obj and msg.date > to_date_obj:
                    continue
                filtered_messages.append(msg)
            messages = filtered_messages
        
        if not messages:
            return "No messages found matching the criteria."
            
        lines = []
        for msg in messages:
            sender = ""
            if msg.sender:
                sender_name = getattr(msg.sender, 'first_name', '') or getattr(msg.sender, 'title', 'Unknown')
                sender = f"{sender_name} | "
                
            lines.append(f"ID: {msg.id} | {sender}Date: {msg.date} | Message: {msg.message or '[Media/No text]'}")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error retrieving messages: {e}"


@mcp.tool()
async def list_chats(chat_type: str = None, limit: int = 20) -> str:
    """
    List available chats with metadata.
    
    Args:
        chat_type: Filter by chat type ('user', 'group', 'channel', or None for all)
        limit: Maximum number of chats to retrieve.
    """
    try:
        dialogs = await client.get_dialogs(limit=limit)
        
        results = []
        for dialog in dialogs:
            entity = dialog.entity
            
            # Filter by type if requested
            current_type = None
            if isinstance(entity, User):
                current_type = "user"
            elif isinstance(entity, Chat):
                current_type = "group"
            elif isinstance(entity, Channel):
                if getattr(entity, 'broadcast', False):
                    current_type = "channel"
                else:
                    current_type = "group"  # Supergroup
            
            if chat_type and current_type != chat_type.lower():
                continue
                
            # Format chat info
            chat_info = f"Chat ID: {entity.id}"
            
            if hasattr(entity, 'title'):
                chat_info += f", Title: {entity.title}"
            elif hasattr(entity, 'first_name'):
                name = f"{entity.first_name}"
                if hasattr(entity, 'last_name') and entity.last_name:
                    name += f" {entity.last_name}"
                chat_info += f", Name: {name}"
                
            chat_info += f", Type: {current_type}"
            
            if hasattr(entity, 'username') and entity.username:
                chat_info += f", Username: @{entity.username}"
                
            # Add unread count if available
            if hasattr(dialog, 'unread_count') and dialog.unread_count > 0:
                chat_info += f", Unread: {dialog.unread_count}"
                
            results.append(chat_info)
            
        if not results:
            return f"No chats found matching the criteria."
            
        return "\n".join(results)
    except Exception as e:
        return f"Error listing chats: {e}"


@mcp.tool()
async def get_chat(chat_id: int) -> str:
    """
    Get detailed information about a specific chat.
    
    Args:
        chat_id: The ID of the chat.
    """
    try:
        entity = await client.get_entity(chat_id)
        
        result = []
        result.append(f"ID: {entity.id}")
        
        if hasattr(entity, 'title'):
            result.append(f"Title: {entity.title}")
            chat_type = "Channel" if getattr(entity, 'broadcast', False) else "Group"
            result.append(f"Type: {chat_type}")
            if hasattr(entity, 'username') and entity.username:
                result.append(f"Username: @{entity.username}")
            if hasattr(entity, 'participants_count'):
                result.append(f"Participants: {entity.participants_count}")
        elif isinstance(entity, User):
            name = f"{entity.first_name}"
            if entity.last_name:
                name += f" {entity.last_name}"
            result.append(f"Name: {name}")
            result.append(f"Type: User")
            if entity.username:
                result.append(f"Username: @{entity.username}")
            if entity.phone:
                result.append(f"Phone: {entity.phone}")
            result.append(f"Bot: {'Yes' if entity.bot else 'No'}")
            result.append(f"Verified: {'Yes' if entity.verified else 'No'}")
            
        # Get last activity if it's a dialog
        try:
            dialogs = await client.get_dialogs(limit=100)
            for dialog in dialogs:
                if dialog.entity.id == chat_id:
                    result.append(f"Unread Messages: {dialog.unread_count}")
                    if dialog.message:
                        last_msg = dialog.message
                        sender = getattr(last_msg.sender, 'first_name', '') or 'Unknown'
                        result.append(f"Last Message: From {sender} at {last_msg.date}")
                        result.append(f"Message: {last_msg.message or '[Media/No text]'}")
                    break
        except:
            pass
            
        return "\n".join(result)
    except Exception as e:
        return f"Error getting chat info: {e}"


@mcp.tool()
async def get_direct_chat_by_contact(contact_query: str) -> str:
    """
    Find a direct chat with a specific contact by name, username, or phone.
    
    Args:
        contact_query: Name, username, or phone number to search for.
    """
    try:
        # First search for the contact
        contacts = await client.get_contacts()
        found_contacts = []
        
        for contact in contacts:
            if not contact:
                continue
                
            name = f"{getattr(contact, 'first_name', '')} {getattr(contact, 'last_name', '')}".strip()
            username = getattr(contact, 'username', '')
            phone = getattr(contact, 'phone', '')
            
            if (contact_query.lower() in name.lower() or 
                (username and contact_query.lower() in username.lower()) or 
                (phone and contact_query in phone)):
                found_contacts.append(contact)
        
        if not found_contacts:
            return f"No contacts found matching '{contact_query}'."
            
        # If we found contacts, look for direct chats with them
        results = []
        dialogs = await client.get_dialogs()
        
        for contact in found_contacts:
            contact_name = f"{getattr(contact, 'first_name', '')} {getattr(contact, 'last_name', '')}".strip()
            for dialog in dialogs:
                if isinstance(dialog.entity, User) and dialog.entity.id == contact.id:
                    chat_info = f"Chat ID: {dialog.entity.id}, Contact: {contact_name}"
                    if getattr(contact, 'username', ''):
                        chat_info += f", Username: @{contact.username}"
                    if dialog.unread_count:
                        chat_info += f", Unread: {dialog.unread_count}"
                    results.append(chat_info)
                    break
        
        if not results:
            found_names = ", ".join([f"{c.first_name} {c.last_name}".strip() for c in found_contacts])
            return f"Found contacts: {found_names}, but no direct chats were found with them."
            
        return "\n".join(results)
    except Exception as e:
        return f"Error finding direct chat: {e}"


@mcp.tool()
async def get_contact_chats(contact_id: int) -> str:
    """
    List all chats involving a specific contact.
    
    Args:
        contact_id: The ID of the contact.
    """
    try:
        # Get contact info
        contact = await client.get_entity(contact_id)
        if not isinstance(contact, User):
            return f"ID {contact_id} is not a user/contact."
            
        contact_name = f"{getattr(contact, 'first_name', '')} {getattr(contact, 'last_name', '')}".strip()
        
        # Find direct chat
        direct_chat = None
        dialogs = await client.get_dialogs()
        
        results = []
        
        # Look for direct chat
        for dialog in dialogs:
            if isinstance(dialog.entity, User) and dialog.entity.id == contact_id:
                chat_info = f"Direct Chat ID: {dialog.entity.id}, Type: Private"
                if dialog.unread_count:
                    chat_info += f", Unread: {dialog.unread_count}"
                results.append(chat_info)
                break
        
        # Look for common groups/channels
        common_chats = []
        try:
            common = await client.get_common_chats(contact)
            for chat in common:
                chat_type = "Channel" if getattr(chat, 'broadcast', False) else "Group"
                chat_info = f"Chat ID: {chat.id}, Title: {chat.title}, Type: {chat_type}"
                results.append(chat_info)
        except:
            results.append("Could not retrieve common groups.")
            
        if not results:
            return f"No chats found with {contact_name} (ID: {contact_id})."
            
        return f"Chats with {contact_name} (ID: {contact_id}):\n" + "\n".join(results)
    except Exception as e:
        return f"Error retrieving contact chats: {e}"


@mcp.tool()
async def get_last_interaction(contact_id: int) -> str:
    """
    Get the most recent message with a contact.
    
    Args:
        contact_id: The ID of the contact.
    """
    try:
        # Get contact info
        contact = await client.get_entity(contact_id)
        if not isinstance(contact, User):
            return f"ID {contact_id} is not a user/contact."
            
        contact_name = f"{getattr(contact, 'first_name', '')} {getattr(contact, 'last_name', '')}".strip()
        
        # Get the last few messages
        messages = await client.get_messages(contact, limit=5)
        
        if not messages:
            return f"No messages found with {contact_name} (ID: {contact_id})."
            
        results = [f"Last interactions with {contact_name} (ID: {contact_id}):"]
        
        for msg in messages:
            sender = "You" if msg.out else contact_name
            message_text = msg.message or "[Media/No text]"
            results.append(f"Date: {msg.date}, From: {sender}, Message: {message_text}")
            
        return "\n".join(results)
    except Exception as e:
        return f"Error retrieving last interaction: {e}"


@mcp.tool()
async def get_message_context(chat_id: int, message_id: int, context_size: int = 3) -> str:
    """
    Retrieve context around a specific message.
    
    Args:
        chat_id: The ID of the chat.
        message_id: The ID of the central message.
        context_size: Number of messages before and after to include.
    """
    try:
        chat = await client.get_entity(chat_id)
        
        # Get messages around the specified message
        messages_before = await client.get_messages(
            chat, 
            limit=context_size,
            max_id=message_id
        )
        
        central_message = await client.get_messages(
            chat,
            ids=message_id
        )
        
        messages_after = await client.get_messages(
            chat,
            limit=context_size,
            min_id=message_id,
            reverse=True
        )
        
        if not central_message:
            return f"Message with ID {message_id} not found in chat {chat_id}."
            
        # Combine messages in chronological order
        all_messages = list(messages_before) + list(central_message) + list(messages_after)
        all_messages.sort(key=lambda m: m.id)
        
        results = [f"Context for message {message_id} in chat {chat_id}:"]
        
        for msg in all_messages:
            sender_name = "Unknown"
            if msg.sender:
                sender_name = getattr(msg.sender, 'first_name', '') or getattr(msg.sender, 'title', 'Unknown')
                
            highlight = " [THIS MESSAGE]" if msg.id == message_id else ""
            results.append(f"ID: {msg.id} | {sender_name} | {msg.date}{highlight}\n{msg.message or '[Media/No text]'}\n")
            
        return "\n".join(results)
    except Exception as e:
        return f"Error retrieving message context: {e}"


if __name__ == "__main__":
    nest_asyncio.apply()

    async def main() -> None:
        try:
            # Start the Telethon client non-interactively
            print("Starting Telegram client...")
            await client.start()
            
            print("Telegram client started. Running MCP server...")
            # Use the asynchronous entrypoint instead of mcp.run()
            await mcp.run_stdio_async()
        except Exception as e:
            print(f"Error starting client: {e}", file=sys.stderr)
            if isinstance(e, sqlite3.OperationalError) and "database is locked" in str(e):
                print("Database lock detected. Please ensure no other instances are running.", file=sys.stderr)
            sys.exit(1)

    asyncio.run(main())
