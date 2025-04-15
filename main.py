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
from telethon import functions
import mimetypes
import logging

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

# Setup logger for error reporting
logging.basicConfig(
    filename='mcp_errors.log',
    level=logging.ERROR,
    format='%(asctime)s %(levelname)s %(name)s %(message)s'
)
logger = logging.getLogger("mcp")

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
    try:
        dialogs = await client.get_dialogs()
        start = (page - 1) * page_size
        end = start + page_size
        if start >= len(dialogs):
            return "Page out of range."
        chats = dialogs[start:end]
        lines = []
        for dialog in chats:
            entity = dialog.entity
            chat_id = entity.id
            title = getattr(entity, "title", None) or getattr(entity, "first_name", "Unknown")
            lines.append(f"Chat ID: {chat_id}, Title: {title}")
        return "\n".join(lines)
    except Exception as e:
        logger.exception(f"get_chats failed (page={page}, page_size={page_size})")
        return "An error occurred (code: GETCHATS-ERR-001). Check mcp_errors.log for details."


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
        offset = (page - 1) * page_size
        messages = await client.get_messages(entity, limit=page_size, add_offset=offset)
        if not messages:
            return "No messages found for this page."
        lines = []
        for msg in messages:
            lines.append(f"ID: {msg.id} | Date: {msg.date} | Message: {msg.message}")
        return "\n".join(lines)
    except Exception as e:
        logger.exception(f"get_messages failed (chat_id={chat_id}, page={page}, page_size={page_size})")
        return "An error occurred (code: GETMSGS-ERR-001). Check mcp_errors.log for details."


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
        await client.send_message(entity, message)
        return "Message sent successfully."
    except Exception as e:
        logger.exception(f"send_message failed (chat_id={chat_id})")
        return "An error occurred (code: SENDMSG-ERR-001). Check mcp_errors.log for details."


@mcp.tool()
async def list_contacts() -> str:
    """
    List all contacts in your Telegram account.
    """
    try:
        result = await client(functions.contacts.GetContactsRequest(hash=0))
        users = result.users
        if not users:
            return "No contacts found."
        lines = []
        for user in users:
            name = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
            username = getattr(user, 'username', '')
            phone = getattr(user, 'phone', '')
            contact_info = f"ID: {user.id}, Name: {name}"
            if username:
                contact_info += f", Username: @{username}"
            if phone:
                contact_info += f", Phone: {phone}"
            lines.append(contact_info)
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing contacts: {e}"


@mcp.tool()
async def search_contacts(query: str) -> str:
    """
    Search for contacts by name, username, or phone number using Telethon's SearchRequest.
    Args:
        query: The search term to look for in contact names, usernames, or phone numbers.
    """
    try:
        result = await client(functions.contacts.SearchRequest(q=query, limit=50))
        users = result.users
        if not users:
            return f"No contacts found matching '{query}'."
        lines = []
        for user in users:
            name = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
            username = getattr(user, 'username', '')
            phone = getattr(user, 'phone', '')
            contact_info = f"ID: {user.id}, Name: {name}"
            if username:
                contact_info += f", Username: @{username}"
            if phone:
                contact_info += f", Phone: {phone}"
            lines.append(contact_info)
        return "\n".join(lines)
    except Exception as e:
        return f"Error searching contacts: {e}"


@mcp.tool()
async def get_contact_ids() -> str:
    """
    Get all contact IDs in your Telegram account.
    """
    try:
        result = await client(functions.contacts.GetContactIDsRequest(hash=0))
        if not result:
            return "No contact IDs found."
        return "Contact IDs: " + ", ".join(str(cid) for cid in result)
    except Exception as e:
        return f"Error getting contact IDs: {e}"


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
        # Fetch all contacts using the correct Telethon method
        result = await client(functions.contacts.GetContactsRequest(hash=0))
        contacts = result.users
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
        # Fix: get_messages(ids=...) returns a single Message, not a list
        if central_message is not None and not isinstance(central_message, list):
            central_message = [central_message]
        elif central_message is None:
            central_message = []
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


@mcp.tool()
async def add_contact(phone: str, first_name: str, last_name: str = "") -> str:
    """
    Add a new contact to your Telegram account.
    Args:
        phone: The phone number of the contact (with country code).
        first_name: The contact's first name.
        last_name: The contact's last name (optional).
    """
    try:
        result = await client(functions.contacts.ImportContactsRequest(
            contacts=[
                functions.contacts.InputPhoneContact(
                    client_id=0,
                    phone=phone,
                    first_name=first_name,
                    last_name=last_name
                )
            ]
        ))
        if result.imported:
            return f"Contact {first_name} {last_name} added successfully."
        else:
            return f"Contact not added. Response: {result.stringify()}"
    except Exception as e:
        return f"Error adding contact: {e}"


@mcp.tool()
async def delete_contact(user_id: int) -> str:
    """
    Delete a contact by user ID.
    Args:
        user_id: The Telegram user ID of the contact to delete.
    """
    try:
        user = await client.get_entity(user_id)
        await client(functions.contacts.DeleteContactsRequest(id=[user]))
        return f"Contact with user ID {user_id} deleted."
    except Exception as e:
        return f"Error deleting contact: {e}"


@mcp.tool()
async def block_user(user_id: int) -> str:
    """
    Block a user by user ID.
    Args:
        user_id: The Telegram user ID to block.
    """
    try:
        user = await client.get_entity(user_id)
        await client(functions.contacts.BlockRequest(id=user))
        return f"User {user_id} blocked."
    except Exception as e:
        return f"Error blocking user: {e}"


@mcp.tool()
async def unblock_user(user_id: int) -> str:
    """
    Unblock a user by user ID.
    Args:
        user_id: The Telegram user ID to unblock.
    """
    try:
        user = await client.get_entity(user_id)
        await client(functions.contacts.UnblockRequest(id=user))
        return f"User {user_id} unblocked."
    except Exception as e:
        return f"Error unblocking user: {e}"


@mcp.tool()
async def get_me() -> str:
    """
    Get your own user information.
    """
    try:
        me = await client.get_me()
        return json.dumps(format_entity(me), indent=2)
    except Exception as e:
        return f"Error getting your info: {e}"


@mcp.tool()
async def create_group(title: str, user_ids: list) -> str:
    """
    Create a new group with the given title and user IDs.
    Args:
        title: The group name.
        user_ids: List of user IDs to add to the group.
    """
    try:
        users = [await client.get_entity(uid) for uid in user_ids]
        result = await client(functions.messages.CreateChatRequest(users=users, title=title))
        return f"Group '{title}' created with ID: {result.chats[0].id}"
    except Exception as e:
        return f"Error creating group: {e}"


@mcp.tool()
async def invite_to_group(group_id: int, user_ids: list) -> str:
    """
    Invite users to a group by group ID.
    Args:
        group_id: The group chat ID.
        user_ids: List of user IDs to invite.
    """
    try:
        users = [await client.get_entity(uid) for uid in user_ids]
        await client(functions.messages.AddChatUserRequest(chat_id=group_id, user_id=users[0], fwd_limit=0))
        # Telethon only allows adding one user at a time for AddChatUserRequest (for basic groups)
        # For supergroups/channels, use InviteToChannelRequest
        return f"Invited users to group {group_id}."
    except Exception as e:
        return f"Error inviting users: {e}"


@mcp.tool()
async def leave_chat(chat_id: int) -> str:
    """
    Leave a group or channel by chat ID.
    Args:
        chat_id: The chat ID to leave.
    """
    try:
        await client(functions.messages.LeaveChatRequest(chat_id=chat_id))
        return f"Left chat {chat_id}."
    except Exception as e:
        return f"Error leaving chat: {e}"


@mcp.tool()
async def get_participants(chat_id: int) -> str:
    """
    List all participants in a group or channel.
    Args:
        chat_id: The group or channel ID.
    """
    try:
        participants = await client.get_participants(chat_id)
        lines = [f"ID: {p.id}, Name: {getattr(p, 'first_name', '')} {getattr(p, 'last_name', '')}" for p in participants]
        return "\n".join(lines)
    except Exception as e:
        return f"Error getting participants: {e}"


@mcp.tool()
async def send_file(chat_id: int, file_path: str, caption: str = None) -> str:
    """
    Send a file to a chat.
    Args:
        chat_id: The chat ID.
        file_path: Absolute path to the file to send (must exist and be readable).
        caption: Optional caption for the file.
    """
    try:
        if not os.path.isfile(file_path):
            return f"File not found: {file_path}"
        if not os.access(file_path, os.R_OK):
            return f"File is not readable: {file_path}"
        entity = await client.get_entity(chat_id)
        await client.send_file(entity, file_path, caption=caption)
        return f"File sent to chat {chat_id}."
    except Exception as e:
        return f"Error sending file: {e}"


@mcp.tool()
async def download_media(chat_id: int, message_id: int, file_path: str) -> str:
    """
    Download media from a message in a chat.
    Args:
        chat_id: The chat ID.
        message_id: The message ID containing the media.
        file_path: Absolute path to save the downloaded file (must be writable).
    """
    try:
        entity = await client.get_entity(chat_id)
        msg = await client.get_messages(entity, ids=message_id)
        if not msg or not msg.media:
            return "No media found in the specified message."
        # Check if directory is writable
        dir_path = os.path.dirname(file_path) or '.'
        if not os.access(dir_path, os.W_OK):
            return f"Directory not writable: {dir_path}"
        await client.download_media(msg, file=file_path)
        if not os.path.isfile(file_path):
            return f"Download failed: file not created at {file_path}"
        return f"Media downloaded to {file_path}."
    except Exception as e:
        return f"Error downloading media: {e}"


@mcp.tool()
async def update_profile(first_name: str = None, last_name: str = None, about: str = None) -> str:
    """
    Update your profile information (name, bio).
    """
    try:
        await client(functions.account.UpdateProfileRequest(
            first_name=first_name,
            last_name=last_name,
            about=about
        ))
        return "Profile updated."
    except Exception as e:
        return f"Error updating profile: {e}"


@mcp.tool()
async def set_profile_photo(file_path: str) -> str:
    """
    Set a new profile photo.
    """
    try:
        await client(functions.photos.UploadProfilePhotoRequest(
            file=await client.upload_file(file_path)
        ))
        return "Profile photo updated."
    except Exception as e:
        return f"Error setting profile photo: {e}"


@mcp.tool()
async def delete_profile_photo() -> str:
    """
    Delete your current profile photo.
    """
    try:
        photos = await client(functions.photos.GetUserPhotosRequest(user_id='me', offset=0, max_id=0, limit=1))
        if not photos.photos:
            return "No profile photo to delete."
        await client(functions.photos.DeletePhotosRequest(id=[photos.photos[0].id]))
        return "Profile photo deleted."
    except Exception as e:
        return f"Error deleting profile photo: {e}"


@mcp.tool()
async def get_privacy_settings() -> str:
    """
    Get your privacy settings.
    """
    try:
        settings = await client(functions.account.GetPrivacyRequest(key='status_timestamp'))
        return str(settings)
    except Exception as e:
        return f"Error getting privacy settings: {e}"


@mcp.tool()
async def set_privacy_settings(key: str, allow_users: list = None, disallow_users: list = None) -> str:
    """
    Set privacy settings (e.g., last seen, phone, etc.).
    key: e.g. 'status_timestamp', 'phone_number', 'profile_photo', 'forwards', 'voice_messages', etc.
    """
    from telethon.tl.types import InputPrivacyKeyStatusTimestamp, InputPrivacyValueAllowUsers, InputPrivacyValueDisallowUsers
    try:
        allow = InputPrivacyValueAllowUsers(users=[await client.get_entity(uid) for uid in (allow_users or [])])
        disallow = InputPrivacyValueDisallowUsers(users=[await client.get_entity(uid) for uid in (disallow_users or [])])
        await client(functions.account.SetPrivacyRequest(
            key=getattr(functions.account, f'InputPrivacyKey{key.title().replace("_", "")}')(),
            rules=[allow, disallow]
        ))
        return f"Privacy settings for {key} updated."
    except Exception as e:
        return f"Error setting privacy: {e}"


@mcp.tool()
async def import_contacts(contacts: list) -> str:
    """
    Import a list of contacts. Each contact should be a dict with phone, first_name, last_name.
    """
    try:
        input_contacts = [functions.contacts.InputPhoneContact(client_id=i, phone=c['phone'], first_name=c['first_name'], last_name=c.get('last_name', '')) for i, c in enumerate(contacts)]
        result = await client(functions.contacts.ImportContactsRequest(contacts=input_contacts))
        return f"Imported {len(result.imported)} contacts."
    except Exception as e:
        return f"Error importing contacts: {e}"


@mcp.tool()
async def export_contacts() -> str:
    """
    Export all contacts as a JSON string.
    """
    try:
        result = await client(functions.contacts.GetContactsRequest(hash=0))
        users = result.users
        return json.dumps([format_entity(u) for u in users], indent=2)
    except Exception as e:
        return f"Error exporting contacts: {e}"


@mcp.tool()
async def get_blocked_users() -> str:
    """
    Get a list of blocked users.
    """
    try:
        result = await client(functions.contacts.GetBlockedRequest(offset=0, limit=100))
        return json.dumps([format_entity(u) for u in result.users], indent=2)
    except Exception as e:
        return f"Error getting blocked users: {e}"


@mcp.tool()
async def create_channel(title: str, about: str = "", megagroup: bool = False) -> str:
    """
    Create a new channel or supergroup.
    """
    try:
        result = await client(functions.channels.CreateChannelRequest(
            title=title,
            about=about,
            megagroup=megagroup
        ))
        return f"Channel '{title}' created with ID: {result.chats[0].id}"
    except Exception as e:
        return f"Error creating channel: {e}"


@mcp.tool()
async def edit_chat_title(chat_id: int, title: str) -> str:
    """
    Edit the title of a chat, group, or channel.
    """
    try:
        await client(functions.messages.EditChatTitleRequest(chat_id=chat_id, title=title))
        return f"Chat {chat_id} title updated."
    except Exception as e:
        return f"Error editing chat title: {e}"


@mcp.tool()
async def edit_chat_photo(chat_id: int, file_path: str) -> str:
    """
    Edit the photo of a chat, group, or channel.
    """
    try:
        file = await client.upload_file(file_path)
        await client(functions.messages.EditChatPhotoRequest(chat_id=chat_id, photo=file))
        return f"Chat {chat_id} photo updated."
    except Exception as e:
        return f"Error editing chat photo: {e}"


@mcp.tool()
async def delete_chat_photo(chat_id: int) -> str:
    """
    Delete the photo of a chat, group, or channel.
    """
    try:
        await client(functions.messages.EditChatPhotoRequest(chat_id=chat_id, photo=None))
        return f"Chat {chat_id} photo deleted."
    except Exception as e:
        return f"Error deleting chat photo: {e}"


@mcp.tool()
async def promote_admin(chat_id: int, user_id: int) -> str:
    """
    Promote a user to admin in a group or channel.
    """
    from telethon.tl.types import ChatAdminRights
    try:
        user = await client.get_entity(user_id)
        await client(functions.channels.EditAdminRequest(
            channel=chat_id,
            user_id=user,
            admin_rights=ChatAdminRights(
                change_info=True, post_messages=True, edit_messages=True, delete_messages=True,
                ban_users=True, invite_users=True, pin_messages=True, add_admins=True, manage_call=True, other=True
            ),
            rank="admin"
        ))
        return f"User {user_id} promoted to admin in chat {chat_id}."
    except Exception as e:
        return f"Error promoting admin: {e}"


@mcp.tool()
async def demote_admin(chat_id: int, user_id: int) -> str:
    """
    Demote an admin to regular user in a group or channel.
    """
    from telethon.tl.types import ChatAdminRights
    try:
        user = await client.get_entity(user_id)
        await client(functions.channels.EditAdminRequest(
            channel=chat_id,
            user_id=user,
            admin_rights=ChatAdminRights(),
            rank=""
        ))
        return f"User {user_id} demoted in chat {chat_id}."
    except Exception as e:
        return f"Error demoting admin: {e}"


@mcp.tool()
async def ban_user(chat_id: int, user_id: int) -> str:
    """
    Ban a user from a group or channel.
    """
    from telethon.tl.types import ChatBannedRights
    import time
    try:
        user = await client.get_entity(user_id)
        banned_rights = ChatBannedRights(until_date=int(time.time()) + 31536000, view_messages=True)
        await client(functions.channels.EditBannedRequest(
            channel=chat_id,
            user_id=user,
            banned_rights=banned_rights
        ))
        return f"User {user_id} banned from chat {chat_id}."
    except Exception as e:
        return f"Error banning user: {e}"


@mcp.tool()
async def unban_user(chat_id: int, user_id: int) -> str:
    """
    Unban a user from a group or channel.
    """
    from telethon.tl.types import ChatBannedRights
    try:
        user = await client.get_entity(user_id)
        banned_rights = ChatBannedRights()
        await client(functions.channels.EditBannedRequest(
            channel=chat_id,
            user_id=user,
            banned_rights=banned_rights
        ))
        return f"User {user_id} unbanned in chat {chat_id}."
    except Exception as e:
        return f"Error unbanning user: {e}"


@mcp.tool()
async def get_admins(chat_id: int) -> str:
    """
    Get all admins in a group or channel.
    """
    try:
        participants = await client.get_participants(chat_id, filter=functions.channels.ParticipantsAdmins())
        return "\n".join([f"ID: {p.id}, Name: {getattr(p, 'first_name', '')} {getattr(p, 'last_name', '')}" for p in participants])
    except Exception as e:
        return f"Error getting admins: {e}"


@mcp.tool()
async def get_banned_users(chat_id: int) -> str:
    """
    Get all banned users in a group or channel.
    """
    try:
        participants = await client.get_participants(chat_id, filter=functions.channels.ParticipantsBanned())
        return "\n".join([f"ID: {p.id}, Name: {getattr(p, 'first_name', '')} {getattr(p, 'last_name', '')}" for p in participants])
    except Exception as e:
        return f"Error getting banned users: {e}"


@mcp.tool()
async def get_invite_link(chat_id: int) -> str:
    """
    Get the invite link for a group or channel.
    """
    try:
        result = await client(functions.messages.ExportChatInviteRequest(chat_id=chat_id))
        return result.link
    except Exception as e:
        return f"Error getting invite link: {e}"


@mcp.tool()
async def join_chat_by_link(link: str) -> str:
    """
    Join a chat by invite link.
    """
    try:
        await client(functions.messages.ImportChatInviteRequest(hash=link.split('/')[-1]))
        return f"Joined chat via link."
    except Exception as e:
        return f"Error joining chat: {e}"


@mcp.tool()
async def export_chat_invite(chat_id: int) -> str:
    """
    Export a chat invite link.
    """
    try:
        result = await client(functions.messages.ExportChatInviteRequest(chat_id=chat_id))
        return result.link
    except Exception as e:
        return f"Error exporting chat invite: {e}"


@mcp.tool()
async def import_chat_invite(hash: str) -> str:
    """
    Import a chat invite by hash.
    """
    try:
        await client(functions.messages.ImportChatInviteRequest(hash=hash))
        return f"Joined chat via invite hash."
    except Exception as e:
        return f"Error importing chat invite: {e}"


@mcp.tool()
async def send_voice(chat_id: int, file_path: str) -> str:
    """
    Send a voice message to a chat. File must be an OGG/OPUS voice note.
    Args:
        chat_id: The chat ID.
        file_path: Absolute path to the OGG/OPUS file.
    """
    try:
        if not os.path.isfile(file_path):
            return f"File not found: {file_path}"
        if not os.access(file_path, os.R_OK):
            return f"File is not readable: {file_path}"
        mime, _ = mimetypes.guess_type(file_path)
        if not (mime and (mime == 'audio/ogg' or file_path.lower().endswith('.ogg') or file_path.lower().endswith('.opus'))):
            return "Voice file must be .ogg or .opus format."
        entity = await client.get_entity(chat_id)
        await client.send_file(entity, file_path, voice_note=True)
        return f"Voice message sent to chat {chat_id}."
    except Exception as e:
        return f"Error sending voice: {e}"


@mcp.tool()
async def forward_message(from_chat_id: int, message_id: int, to_chat_id: int) -> str:
    """
    Forward a message from one chat to another.
    """
    try:
        from_entity = await client.get_entity(from_chat_id)
        to_entity = await client.get_entity(to_chat_id)
        await client.forward_messages(to_entity, message_id, from_entity)
        return f"Message {message_id} forwarded from {from_chat_id} to {to_chat_id}."
    except Exception as e:
        return f"Error forwarding message: {e}"


@mcp.tool()
async def edit_message(chat_id: int, message_id: int, new_text: str) -> str:
    """
    Edit a message you sent.
    """
    try:
        entity = await client.get_entity(chat_id)
        await client.edit_message(entity, message_id, new_text)
        return f"Message {message_id} edited."
    except Exception as e:
        return f"Error editing message: {e}"


@mcp.tool()
async def delete_message(chat_id: int, message_id: int) -> str:
    """
    Delete a message by ID.
    """
    try:
        entity = await client.get_entity(chat_id)
        await client.delete_messages(entity, message_id)
        return f"Message {message_id} deleted."
    except Exception as e:
        return f"Error deleting message: {e}"


@mcp.tool()
async def pin_message(chat_id: int, message_id: int) -> str:
    """
    Pin a message in a chat.
    """
    try:
        entity = await client.get_entity(chat_id)
        await client.pin_message(entity, message_id)
        return f"Message {message_id} pinned in chat {chat_id}."
    except Exception as e:
        return f"Error pinning message: {e}"


@mcp.tool()
async def unpin_message(chat_id: int, message_id: int) -> str:
    """
    Unpin a message in a chat.
    """
    try:
        entity = await client.get_entity(chat_id)
        await client.unpin_message(entity, message_id)
        return f"Message {message_id} unpinned in chat {chat_id}."
    except Exception as e:
        return f"Error unpinning message: {e}"


@mcp.tool()
async def mark_as_read(chat_id: int) -> str:
    """
    Mark all messages as read in a chat.
    """
    try:
        entity = await client.get_entity(chat_id)
        await client.send_read_acknowledge(entity)
        return f"Marked all messages as read in chat {chat_id}."
    except Exception as e:
        return f"Error marking as read: {e}"


@mcp.tool()
async def reply_to_message(chat_id: int, message_id: int, text: str) -> str:
    """
    Reply to a specific message in a chat.
    """
    try:
        entity = await client.get_entity(chat_id)
        await client.send_message(entity, text, reply_to=message_id)
        return f"Replied to message {message_id} in chat {chat_id}."
    except Exception as e:
        return f"Error replying to message: {e}"


@mcp.tool()
async def upload_file(file_path: str) -> str:
    """
    Upload a file to Telegram servers (returns file handle as string, not a file path).
    Args:
        file_path: Absolute path to the file to upload (must exist and be readable).
    """
    try:
        if not os.path.isfile(file_path):
            return f"File not found: {file_path}"
        if not os.access(file_path, os.R_OK):
            return f"File is not readable: {file_path}"
        file = await client.upload_file(file_path)
        return str(file)
    except Exception as e:
        return f"Error uploading file: {e}"


@mcp.tool()
async def get_media_info(chat_id: int, message_id: int) -> str:
    """
    Get info about media in a message.
    Args:
        chat_id: The chat ID.
        message_id: The message ID.
    """
    try:
        entity = await client.get_entity(chat_id)
        msg = await client.get_messages(entity, ids=message_id)
        if not msg or not msg.media:
            return "No media found in the specified message."
        return str(msg.media)
    except Exception as e:
        return f"Error getting media info: {e}"


@mcp.tool()
async def search_public_chats(query: str) -> str:
    """
    Search for public chats, channels, or bots by username or title.
    """
    try:
        result = await client(functions.contacts.SearchRequest(q=query, limit=20))
        return json.dumps([format_entity(u) for u in result.users], indent=2)
    except Exception as e:
        return f"Error searching public chats: {e}"


@mcp.tool()
async def search_messages(chat_id: int, query: str, limit: int = 20) -> str:
    """
    Search for messages in a chat by text.
    """
    try:
        entity = await client.get_entity(chat_id)
        messages = await client.get_messages(entity, limit=limit, search=query)
        return "\n".join([f"ID: {m.id} | {m.date} | {m.message}" for m in messages])
    except Exception as e:
        return f"Error searching messages: {e}"


@mcp.tool()
async def resolve_username(username: str) -> str:
    """
    Resolve a username to a user or chat ID.
    """
    try:
        result = await client(functions.contacts.ResolveUsernameRequest(username=username))
        return str(result)
    except Exception as e:
        return f"Error resolving username: {e}"


@mcp.tool()
async def mute_chat(chat_id: int) -> str:
    """
    Mute notifications for a chat.
    """
    try:
        await client(functions.account.UpdateNotifySettingsRequest(
            peer=await client.get_entity(chat_id),
            settings=functions.account.InputPeerNotifySettings(mute_until=2**31-1)
        ))
        return f"Chat {chat_id} muted."
    except Exception as e:
        return f"Error muting chat: {e}"


@mcp.tool()
async def unmute_chat(chat_id: int) -> str:
    """
    Unmute notifications for a chat.
    """
    try:
        await client(functions.account.UpdateNotifySettingsRequest(
            peer=await client.get_entity(chat_id),
            settings=functions.account.InputPeerNotifySettings(mute_until=0)
        ))
        return f"Chat {chat_id} unmuted."
    except Exception as e:
        return f"Error unmuting chat: {e}"


@mcp.tool()
async def archive_chat(chat_id: int) -> str:
    """
    Archive a chat.
    """
    try:
        await client(functions.messages.ToggleDialogPinRequest(
            peer=await client.get_entity(chat_id),
            pinned=True
        ))
        return f"Chat {chat_id} archived."
    except Exception as e:
        return f"Error archiving chat: {e}"


@mcp.tool()
async def unarchive_chat(chat_id: int) -> str:
    """
    Unarchive a chat.
    """
    try:
        await client(functions.messages.ToggleDialogPinRequest(
            peer=await client.get_entity(chat_id),
            pinned=False
        ))
        return f"Chat {chat_id} unarchived."
    except Exception as e:
        return f"Error unarchiving chat: {e}"


@mcp.tool()
async def get_sticker_sets() -> str:
    """
    Get all sticker sets.
    """
    try:
        result = await client(functions.messages.GetAllStickersRequest(hash=0))
        return json.dumps([s.title for s in result.sets], indent=2)
    except Exception as e:
        return f"Error getting sticker sets: {e}"


@mcp.tool()
async def send_sticker(chat_id: int, file_path: str) -> str:
    """
    Send a sticker to a chat. File must be a valid .webp sticker file.
    Args:
        chat_id: The chat ID.
        file_path: Absolute path to the .webp sticker file.
    """
    try:
        if not os.path.isfile(file_path):
            return f"Sticker file not found: {file_path}"
        if not os.access(file_path, os.R_OK):
            return f"Sticker file is not readable: {file_path}"
        if not file_path.lower().endswith('.webp'):
            return "Sticker file must be a .webp file."
        entity = await client.get_entity(chat_id)
        await client.send_file(entity, file_path, force_document=False)
        return f"Sticker sent to chat {chat_id}."
    except Exception as e:
        return f"Error sending sticker: {e}"


@mcp.tool()
async def get_gif_search(query: str, limit: int = 10) -> str:
    """
    Search for GIFs by query. Returns a list of Telegram document IDs (not file paths).
    Args:
        query: Search term for GIFs.
        limit: Max number of GIFs to return.
    """
    try:
        result = await client(functions.messages.SearchGifsRequest(q=query, offset_id=0, limit=limit))
        if not result.gifs:
            return "No GIFs found for this query."
        return json.dumps([g.document.id for g in result.gifs], indent=2)
    except Exception as e:
        return f"Error searching GIFs: {e}"


@mcp.tool()
async def send_gif(chat_id: int, gif_id: int) -> str:
    """
    Send a GIF to a chat by Telegram GIF document ID (not a file path).
    Args:
        chat_id: The chat ID.
        gif_id: Telegram document ID for the GIF (from get_gif_search).
    """
    try:
        if not isinstance(gif_id, int):
            return "gif_id must be a Telegram document ID (integer), not a file path. Use get_gif_search to find IDs."
        entity = await client.get_entity(chat_id)
        await client.send_file(entity, gif_id)
        return f"GIF sent to chat {chat_id}."
    except Exception as e:
        return f"Error sending GIF: {e}"


@mcp.tool()
async def get_bot_info(bot_username: str) -> str:
    """
    Get information about a bot by username.
    """
    try:
        result = await client(functions.users.GetFullUserRequest(id=bot_username))
        return json.dumps(result.to_dict(), indent=2)
    except Exception as e:
        return f"Error getting bot info: {e}"


@mcp.tool()
async def set_bot_commands(bot_username: str, commands: list) -> str:
    """
    Set bot commands for a bot you own.
    """
    from telethon.tl.types import BotCommand
    try:
        await client(functions.bots.SetBotCommandsRequest(
            scope=bot_username,
            lang_code='',
            commands=[BotCommand(command=c['command'], description=c['description']) for c in commands]
        ))
        return f"Bot commands set for {bot_username}."
    except Exception as e:
        return f"Error setting bot commands: {e}"


@mcp.tool()
async def get_history(chat_id: int, limit: int = 100) -> str:
    """
    Get full chat history (up to limit).
    """
    try:
        entity = await client.get_entity(chat_id)
        messages = await client.get_messages(entity, limit=limit)
        return "\n".join([f"ID: {m.id} | {m.date} | {m.message}" for m in messages])
    except Exception as e:
        return f"Error getting history: {e}"


@mcp.tool()
async def get_user_photos(user_id: int, limit: int = 10) -> str:
    """
    Get profile photos of a user.
    """
    try:
        user = await client.get_entity(user_id)
        photos = await client(functions.photos.GetUserPhotosRequest(user_id=user, offset=0, max_id=0, limit=limit))
        return json.dumps([p.id for p in photos.photos], indent=2)
    except Exception as e:
        return f"Error getting user photos: {e}"


@mcp.tool()
async def get_user_status(user_id: int) -> str:
    """
    Get the online status of a user.
    """
    try:
        user = await client.get_entity(user_id)
        return str(user.status)
    except Exception as e:
        return f"Error getting user status: {e}"


@mcp.tool()
async def get_recent_actions(chat_id: int) -> str:
    """
    Get recent admin actions (admin log) in a group or channel.
    """
    try:
        result = await client(functions.channels.GetAdminLogRequest(channel=chat_id, q="", events_filter=None, admins=[], max_id=0, min_id=0, limit=20))
        return json.dumps([e.to_dict() for e in result.events], indent=2)
    except Exception as e:
        return f"Error getting recent actions: {e}"


@mcp.tool()
async def get_pinned_messages(chat_id: int) -> str:
    """
    Get all pinned messages in a chat.
    """
    try:
        entity = await client.get_entity(chat_id)
        messages = await client.get_messages(entity, filter=functions.messages.FilterPinned())
        return "\n".join([f"ID: {m.id} | {m.date} | {m.message}" for m in messages])
    except Exception as e:
        return f"Error getting pinned messages: {e}"


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
                print(
                    "Database lock detected. Please ensure no other instances are running.",
                    file=sys.stderr
                )
            sys.exit(1)

    asyncio.run(main())
