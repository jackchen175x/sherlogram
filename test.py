import os
import sys
import asyncio
import nest_asyncio
from dotenv import load_dotenv
import logging
import random
import string
import json
from datetime import datetime, timedelta

# Assume main.py is in the same directory or adjust path accordingly
from main import (
    client, mcp, 
    get_chats, get_messages, send_message, list_contacts, search_contacts,
    get_contact_ids, list_messages, list_chats, get_chat, get_direct_chat_by_contact,
    get_contact_chats, get_last_interaction, get_message_context, add_contact,
    delete_contact, block_user, unblock_user, get_me, create_group,
    invite_to_group, leave_chat, get_participants, send_file, download_media,
    update_profile, set_profile_photo, delete_profile_photo, get_privacy_settings,
    set_privacy_settings, import_contacts, export_contacts, get_blocked_users,
    create_channel, edit_chat_title, edit_chat_photo, delete_chat_photo,
    promote_admin, demote_admin, ban_user, unban_user, get_admins,
    get_banned_users, get_invite_link, join_chat_by_link, export_chat_invite,
    import_chat_invite, send_voice, forward_message, edit_message,
    delete_message, pin_message, unpin_message, mark_as_read, reply_to_message,
    upload_file, get_media_info, search_public_chats, search_messages,
    resolve_username, mute_chat, unmute_chat, archive_chat, unarchive_chat,
    get_sticker_sets, send_sticker, get_gif_search, send_gif, get_bot_info,
    set_bot_commands, get_history, get_user_photos, get_user_status,
    get_recent_actions, get_pinned_messages
)
# Import specific telethon types needed for tests
from telethon.errors.rpcerrorlist import UserNotParticipantError
from telethon.tl import types

load_dotenv()

logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='.log', # Log to .log file
    filemode='w' # Overwrite the log file each time
)
logger = logging.getLogger("TelegramToolTester")
logger.info("Logging configured to .log file.") # Force file creation early

# --- Test Configuration ---
# Set these environment variables before running the test script
TEST_CHAT_ID = int(os.getenv("TEST_CHAT_ID", "0")) # A safe chat ID (e.g., Saved Messages or a test group)
TEST_SUPERGROUP_ID = int(os.getenv("TEST_SUPERGROUP_ID", "0")) # ID of a test supergroup/channel you own/admin
TEST_USER_ID = int(os.getenv("TEST_USER_ID", "0")) # ID of a test user account (NOT a real person unless they consent!)
TEST_USERNAME = os.getenv("TEST_USERNAME", "") # Username of the test user
TEST_CONTACT_PHONE = os.getenv("TEST_CONTACT_PHONE", "") # Phone number for add_contact test (e.g., +15551234567)
TEST_CONTACT_FNAME = os.getenv("TEST_CONTACT_FNAME", "Test")
TEST_CONTACT_LNAME = os.getenv("TEST_CONTACT_LNAME", "Contact")
TEST_FILE_PATH = os.getenv("TEST_FILE_PATH", "test_upload.txt") # Path to a dummy file for upload/send tests
TEST_PHOTO_PATH = os.getenv("TEST_PHOTO_PATH", "test_photo.jpg") # Path to a dummy photo file
TEST_VOICE_PATH = os.getenv("TEST_VOICE_PATH", "test_voice.ogg") # Path to a dummy ogg voice file
TEST_STICKER_PATH = os.getenv("TEST_STICKER_PATH", "test_sticker.webp") # Path to a dummy webp sticker
TEST_BOT_USERNAME = os.getenv("TEST_BOT_USERNAME", "") # Username of a bot you own
TEST_INVITE_LINK_HASH = os.getenv("TEST_INVITE_LINK_HASH", "") # Hash from a valid invite link

# Create dummy files if they don't exist
if not os.path.exists(TEST_FILE_PATH):
    with open(TEST_FILE_PATH, "w") as f:
        f.write("This is a test file.")
if not os.path.exists(TEST_PHOTO_PATH):
    logger.warning(f"Test photo file not found: {TEST_PHOTO_PATH}. Some tests might fail.")
if not os.path.exists(TEST_VOICE_PATH):
    logger.warning(f"Test voice file not found: {TEST_VOICE_PATH}. send_voice test will fail.")
if not os.path.exists(TEST_STICKER_PATH):
     logger.warning(f"Test sticker file not found: {TEST_STICKER_PATH}. send_sticker test will fail.")


async def run_test(tool_func, description, **kwargs):
    logger.info(f"--- Testing: {description} ({tool_func.__name__}) ---")
    logger.info(f"Params: {kwargs}")
    try:
        result = await tool_func(**kwargs)
        logger.info(f"Result: {result}")
        return result
    except Exception as e:
        logger.error(f"Error during {tool_func.__name__}: {e}", exc_info=True)
        return f"TEST FAILED: {e}"

async def run_all_tests():
    global TEST_CHAT_ID # Declare intention to modify the global variable
    if not await client.is_user_authorized():
        logger.error("Client not authorized. Please run main.py first to log in.")
        return

    logger.info("Starting Telegram Tool Tests...")
    me = await client.get_me()
    me_info = await get_me() # Use the tool version
    logger.info(f"Running tests as: {me.first_name} (ID: {me.id})")

    if not TEST_CHAT_ID:
         TEST_CHAT_ID = me.id # Default to Saved Messages if not set
         logger.warning(f"TEST_CHAT_ID not set, defaulting to Saved Messages ({TEST_CHAT_ID}).")
    
    # --- Basic Info & Chat Listing ---
    logger.info("--- Running Basic Info & Listing Tests ---")
    await run_test(get_me, "Get own user info")
    await run_test(list_chats, "List recent chats", limit=5)
    await run_test(list_chats, "List groups", chat_type='group', limit=5)
    await run_test(list_chats, "List channels", chat_type='channel', limit=5)
    await run_test(list_chats, "List users", chat_type='user', limit=5)
    
    # --- Test the basic get_chats function ---
    await run_test(get_chats, "Get chats tool (paginated, page 1)", page=1, page_size=5)

    # --- Specific Chat Operations (using TEST_CHAT_ID) ---
    message_id_to_test = None
    if TEST_CHAT_ID:
        logger.info(f"--- Running tests on Chat ID: {TEST_CHAT_ID} ---")
        await run_test(get_chat, "Get info for test chat", chat_id=TEST_CHAT_ID)
        await run_test(get_history, "Get history for test chat", chat_id=TEST_CHAT_ID, limit=5)
        await run_test(get_messages, "Get messages tool (paginated)", chat_id=TEST_CHAT_ID, page=1, page_size=5)
        
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        await run_test(list_messages, "List messages tool (filtered date)", chat_id=TEST_CHAT_ID, limit=10, from_date=yesterday.strftime("%Y-%m-%d"), to_date=today.strftime("%Y-%m-%d"))
        await run_test(list_messages, "List messages tool (search)", chat_id=TEST_CHAT_ID, limit=10, search_query="Test")

        # Send a test message to get IDs for subsequent tests
        sent_msg_result = await run_test(send_message, "Send test message", chat_id=TEST_CHAT_ID, message=f"MCP Test Message {random.randint(1000, 9999)}")
        if "successfully" in str(sent_msg_result).lower():
             try:
                 # Fetch the last message sent by self (hopefully the test message)
                 async for msg in client.iter_messages(TEST_CHAT_ID, limit=5, from_user='me'):
                     # Check if it's the message we likely just sent
                     if "MCP Test Message" in msg.text:
                         message_id_to_test = msg.id
                         logger.info(f"Using message ID {message_id_to_test} for further tests.")
                         break
                 if not message_id_to_test: # Fallback if specific message not found
                      last_msgs = await client.get_messages(TEST_CHAT_ID, limit=1)
                      if last_msgs:
                           message_id_to_test = last_msgs[0].id
                           logger.warning(f"Could not find specific test message, using last message ID: {message_id_to_test}")

             except Exception as e:
                 logger.error(f"Could not get last message ID: {e}")
        
        if message_id_to_test:
            logger.info(f"--- Running Message-Specific Tests (ID: {message_id_to_test}) ---")
            await run_test(edit_message, "Edit test message", chat_id=TEST_CHAT_ID, message_id=message_id_to_test, new_text="MCP Test Message (edited)")
            await run_test(get_message_context, "Get context for test message", chat_id=TEST_CHAT_ID, message_id=message_id_to_test, context_size=1)
            await run_test(reply_to_message, "Reply to test message", chat_id=TEST_CHAT_ID, message_id=message_id_to_test, text="Test Reply")
            await run_test(pin_message, "Pin test message", chat_id=TEST_CHAT_ID, message_id=message_id_to_test)
            await asyncio.sleep(2) # Give time for pin to register
            await run_test(get_pinned_messages, "Get pinned messages", chat_id=TEST_CHAT_ID)
            await run_test(unpin_message, "Unpin test message", chat_id=TEST_CHAT_ID, message_id=message_id_to_test)
            # Forwarding (Careful: forwards TO TEST_CHAT_ID FROM TEST_CHAT_ID)
            await run_test(forward_message, "Forward test message", from_chat_id=TEST_CHAT_ID, message_id=message_id_to_test, to_chat_id=TEST_CHAT_ID)
            await run_test(delete_message, "Delete test message", chat_id=TEST_CHAT_ID, message_id=message_id_to_test)
        else:
             logger.warning("Could not obtain a message ID for message-specific tests.")

        await run_test(search_messages, "Search for 'Test' in test chat", chat_id=TEST_CHAT_ID, query="Test", limit=5)
        await run_test(mark_as_read, "Mark test chat as read", chat_id=TEST_CHAT_ID)
        
        # File Operations
        logger.info("--- Running File Operation Tests ---")
        await run_test(send_file, "Send test file", chat_id=TEST_CHAT_ID, file_path=TEST_FILE_PATH, caption="Test File")
        await run_test(upload_file, "Upload test file", file_path=TEST_FILE_PATH)
        # Find a message with media to test download/info
        media_message_id = None
        media_msg_obj = None
        async for msg in client.iter_messages(TEST_CHAT_ID, limit=20):
             if msg.media:
                 media_message_id = msg.id
                 media_msg_obj = msg
                 logger.info(f"Found media message ID {media_message_id} to test download/info.")
                 break
        if media_message_id and media_msg_obj:
             await run_test(get_media_info, "Get media info", chat_id=TEST_CHAT_ID, message_id=media_message_id)
             # Use a more specific download path based on filename if possible
             download_filename = getattr(media_msg_obj.media, 'document', None)
             if download_filename:
                  download_filename = getattr(download_filename, 'attributes', [None])[0]
                  if download_filename:
                      download_filename = getattr(download_filename, 'file_name', None)
             download_path = f"test_download_{download_filename or media_message_id}.tmp"

             await run_test(download_media, "Download media", chat_id=TEST_CHAT_ID, message_id=media_message_id, file_path=download_path)
             if os.path.exists(download_path):
                 try:
                     os.remove(download_path)
                     logger.info(f"Cleaned up downloaded file: {download_path}")
                 except OSError as e:
                      logger.error(f"Error removing downloaded file {download_path}: {e}")
             else:
                  logger.warning(f"Could not find downloaded file to clean up: {download_path}")
        else:
             logger.warning("No media message found in recent history to test download/info.")

        # Voice/Sticker/GIF (check paths exist)
        logger.info("--- Running Media Send Tests ---")
        if os.path.exists(TEST_VOICE_PATH):
            await run_test(send_voice, "Send voice message", chat_id=TEST_CHAT_ID, file_path=TEST_VOICE_PATH)
        
        # Enhanced sticker testing
        logger.info("--- Running Enhanced Sticker Tests ---")
        if os.path.exists(TEST_STICKER_PATH):
            await run_test(send_sticker, "Send sticker file", chat_id=TEST_CHAT_ID, file_path=TEST_STICKER_PATH)
        else:
            logger.warning(f"Test sticker file not found at {TEST_STICKER_PATH}")
            
        # Test sticker set retrieval
        sticker_sets = await run_test(get_sticker_sets, "Get all available sticker sets")
        
        # Try to parse sticker set info
        try:
            # Parse any available sticker info
            if sticker_sets and "sets" in sticker_sets:
                logger.info("Successfully retrieved sticker sets")
            elif sticker_sets and len(sticker_sets) > 2:  # JSON output has at least []
                logger.info(f"Retrieved sticker sets data: {sticker_sets[:100]}...")
            else:
                logger.warning("No sticker sets found or empty response")
                
            # If we're on a test account with limited stickers, we could:
            # 1. Add a sticker set (not implemented in our tools)
            # 2. Remove a sticker set (not implemented in our tools)
            logger.info("Note: Adding/removing sticker sets requires additional tools not currently implemented")
        except Exception as e:
            logger.error(f"Error in sticker set processing: {e}")
        
        # GIF testing
        gif_search_result = await run_test(get_gif_search, "Search for GIFs", query="hello", limit=1)
        try:
            gif_ids = json.loads(gif_search_result)
            if gif_ids:
                 gif_id = gif_ids[0]
                 await run_test(send_gif, "Send GIF by ID", chat_id=TEST_CHAT_ID, gif_id=gif_id)
            else:
                 logger.warning("No GIF IDs returned from search.")
        except Exception as e:
            logger.warning(f"Could not parse or send GIF ID from search result: {e}")
        
        # Mute/Archive
        logger.info("--- Running Chat State Tests ---")
        await run_test(mute_chat, "Mute test chat", chat_id=TEST_CHAT_ID)
        await asyncio.sleep(1)
        await run_test(unmute_chat, "Unmute test chat", chat_id=TEST_CHAT_ID)
        await asyncio.sleep(1)
        # Archive/Unarchive - Check if ToggleDialogPinRequest exists and test
        if hasattr(types.messages, 'ToggleDialogPinRequest'):
             logger.warning("--- Testing Archive/Unarchive (May depend on Telethon version) ---")
             await run_test(archive_chat, "Archive test chat", chat_id=TEST_CHAT_ID)
             await asyncio.sleep(1)
             await run_test(unarchive_chat, "Unarchive test chat", chat_id=TEST_CHAT_ID)
             await asyncio.sleep(1)
        else:
             logger.warning("ToggleDialogPinRequest not found, skipping archive/unarchive tests.")

    # --- Contact Operations ---
    logger.info("--- Running Contact Operations Tests ---")
    await run_test(list_contacts, "List contacts")
    await run_test(export_contacts, "Export contacts to JSON")
    
    contact_to_delete_id = None
    if TEST_CONTACT_PHONE and TEST_CONTACT_FNAME:
        logger.warning("--- Running Add/Delete Contact Test (requires valid phone number) ---")
        # Check if contact already exists
        contact_exists = False
        contacts_list = await list_contacts()
        if TEST_CONTACT_PHONE in str(contacts_list):
             logger.warning(f"Contact with phone {TEST_CONTACT_PHONE} seems to exist. Skipping add.")
             # Try to find ID for deletion test anyway
             try:
                 lines = str(contacts_list).split('\n')
                 for line in lines:
                     if TEST_CONTACT_PHONE in line:
                         contact_to_delete_id = int(line.split(',')[0].split(':')[1].strip())
                         logger.info(f"Found existing contact ID for deletion test: {contact_to_delete_id}")
                         break
             except Exception as e:
                 logger.error(f"Could not parse existing contact ID: {e}")
             contact_exists = True

        if not contact_exists:
            add_result = await run_test(add_contact, "Add test contact", phone=TEST_CONTACT_PHONE, first_name=TEST_CONTACT_FNAME, last_name=TEST_CONTACT_LNAME)
            await asyncio.sleep(5) # Give time for contact to sync
            if "added successfully" in str(add_result).lower():
                # Try to find the added contact to get its ID for deletion
                search_res = await run_test(search_contacts, "Search for added contact", query=TEST_CONTACT_PHONE)
                try:
                     lines = str(search_res).split('\n')
                     for line in lines:
                          if TEST_CONTACT_PHONE in line and f"Name: {TEST_CONTACT_FNAME}" in line:
                               contact_to_delete_id = int(line.split(',')[0].split(':')[1].strip())
                               logger.info(f"Found added contact ID for deletion: {contact_to_delete_id}")
                               break
                except Exception as e:
                     logger.error(f"Could not parse contact ID from search result: {e}")
            else:
                 logger.warning("Add contact failed or did not return success message.")
        
        if contact_to_delete_id:
             await run_test(get_direct_chat_by_contact, "Get direct chat by contact's phone", contact_query=TEST_CONTACT_PHONE)
             await run_test(get_contact_chats, "Get chats involving contact", contact_id=contact_to_delete_id)
             logger.warning(f"--- Proceeding to delete contact ID: {contact_to_delete_id} ---")
             await run_test(delete_contact, "Delete test contact", user_id=contact_to_delete_id)
        else:
             logger.warning("Could not find contact by phone number to test deletion/other ops.")

    if TEST_USERNAME:
        await run_test(search_contacts, "Search contacts by username", query=TEST_USERNAME)
        await run_test(resolve_username, "Resolve test username", username=TEST_USERNAME)
        await run_test(get_direct_chat_by_contact, "Get direct chat by test username", contact_query=TEST_USERNAME)

    await run_test(get_contact_ids, "Get contact IDs")
    # import_contacts test requires a list of dicts, harder to setup via env vars
    # logger.warning("Skipping import_contacts test - requires manual setup.")
    # await run_test(import_contacts, "Import contacts", contacts=[{'phone': '+1555...', 'first_name': ...}])
    # Clarification: import_contacts test is skipped as it requires complex setup (list of dictionaries) 
    # which is difficult to manage solely through environment variables for automated testing.

    # --- User Interaction (Requires TEST_USER_ID) ---
    if TEST_USER_ID:
        logger.info(f"--- Running User Interaction Tests (User ID: {TEST_USER_ID}) ---")
        await run_test(get_user_status, "Get test user status", user_id=TEST_USER_ID)
        await run_test(get_user_photos, "Get test user photos", user_id=TEST_USER_ID, limit=1)
        # Check if the user is a contact before running contact-specific tests
        is_contact = False
        contacts_res = await list_contacts()
        if f"ID: {TEST_USER_ID}" in str(contacts_res):
             is_contact = True
             logger.info(f"User {TEST_USER_ID} is a contact.")
             await run_test(get_last_interaction, "Get last interaction with test user contact", contact_id=TEST_USER_ID)
             await run_test(get_contact_chats, "Get chats involving test user contact", contact_id=TEST_USER_ID)
        else:
             logger.warning(f"User {TEST_USER_ID} is not a contact. Skipping contact-specific tests.")
        
        # Block/Unblock
        await run_test(block_user, "Block test user", user_id=TEST_USER_ID)
        await asyncio.sleep(1)
        await run_test(get_blocked_users, "Get blocked users (check if test user is present)")
        await run_test(unblock_user, "Unblock test user", user_id=TEST_USER_ID)
        await asyncio.sleep(1)

    # --- Supergroup/Channel Operations (Requires TEST_SUPERGROUP_ID and TEST_USER_ID) ---
    created_group_id = None
    created_channel_id = None
    if TEST_USER_ID:
         # Create Group Test (Requires TEST_USER_ID)
         logger.warning("--- Running Group/Channel Creation Test ---")
         create_group_res = await run_test(create_group, "Create test group with test user", title=f"MCP Test Group {random.randint(100,999)}", user_ids=[TEST_USER_ID])
         try:
             if "created with ID" in create_group_res:
                 created_group_id = int(create_group_res.split(':')[-1].strip())
                 logger.info(f"Created group ID: {created_group_id}")
                 await asyncio.sleep(2)
                 logger.warning(f"--- Leaving newly created group: {created_group_id} ---")
                 await run_test(leave_chat, "Leave newly created group", chat_id=created_group_id)
         except Exception as e:
             logger.error(f"Failed to parse or leave created group: {e}")
         
         # Create Channel Test (No additional users needed initially)
         create_channel_res = await run_test(create_channel, "Create test channel", title=f"MCP Test Channel {random.randint(100,999)}", about="Test channel created by MCP", megagroup=False)
         try:
             if "created with ID" in create_channel_res:
                 created_channel_id = int(create_channel_res.split(':')[-1].strip())
                 logger.info(f"Created channel ID: {created_channel_id}")
                 await asyncio.sleep(2)
                 logger.warning(f"--- Leaving newly created channel: {created_channel_id} ---")
                 await run_test(leave_chat, "Leave newly created channel", chat_id=created_channel_id)
         except Exception as e:
             logger.error(f"Failed to parse or leave created channel: {e}")

    if TEST_SUPERGROUP_ID:
        logger.info(f"--- Running Supergroup/Channel Operations Tests (Chat ID: {TEST_SUPERGROUP_ID}) ---")
        await run_test(get_chat, "Get test supergroup info", chat_id=TEST_SUPERGROUP_ID)
        await run_test(get_participants, "Get participants of test supergroup", chat_id=TEST_SUPERGROUP_ID)
        await run_test(get_admins, "Get admins of test supergroup", chat_id=TEST_SUPERGROUP_ID)
        await run_test(get_banned_users, "Get banned users of test supergroup", chat_id=TEST_SUPERGROUP_ID)
        await run_test(get_recent_actions, "Get recent actions for supergroup", chat_id=TEST_SUPERGROUP_ID)
        await run_test(get_invite_link, "Get invite link for supergroup", chat_id=TEST_SUPERGROUP_ID)
        await run_test(export_chat_invite, "Export chat invite for supergroup", chat_id=TEST_SUPERGROUP_ID)
        
        if TEST_INVITE_LINK_HASH:
             logger.warning(f"--- Running Join Chat by Invite Hash Test (Requires valid HASH: {TEST_INVITE_LINK_HASH}) ---")
             # Extract hash if full URL is provided
             invite_hash = TEST_INVITE_LINK_HASH
             if invite_hash.startswith("https://t.me/+"):
                 invite_hash = invite_hash.split("+", 1)[1]
                 logger.info(f"Extracted hash from URL: {invite_hash}")
             # This will handle various cases, including invalid/expired hash or already a member
             import_res = await run_test(import_chat_invite, "Join chat via import hash", hash=invite_hash)
             
             # Check if the response indicates already a member or successful join
             already_member = "already a member" in import_res.lower()
             success_join = "successfully joined" in import_res.lower()
             logger.info(f"Invite result: {'Already a member' if already_member else 'Successfully joined' if success_join else 'Failed to join'}")
             
             # Also test the full URL version if appropriate
             if TEST_INVITE_LINK_HASH.startswith("https://"):
                 await run_test(join_chat_by_link, "Join chat via full link", link=TEST_INVITE_LINK_HASH)
                 
             # If we successfully joined a chat, we should leave it to clean up
             if success_join and "chat:" in import_res:
                 try:
                     # Extract chat ID from success message if possible
                     chat_title = import_res.split("chat:", 1)[1].strip()
                     logger.warning(f"Attempting to find and leave newly joined chat: '{chat_title}'")
                     
                     # Try to find the chat ID by matching the title
                     async for dialog in client.iter_dialogs(limit=10):  # Check recent dialogs
                         if dialog.name == chat_title:
                             logger.info(f"Found chat to leave: {dialog.name} (ID: {dialog.id})")
                             await run_test(leave_chat, "Leave newly joined chat", chat_id=dialog.id)
                             break
                 except Exception as leave_err:
                     logger.error(f"Failed to leave newly joined chat: {leave_err}")
        else:
             logger.warning("TEST_INVITE_LINK_HASH not set. Skipping join/import tests.")

        if TEST_USER_ID:
            # Ban/Unban/Invite/Promote tests (Use with EXTREME caution)
            logger.warning(f"--- Running potentially disruptive tests on supergroup {TEST_SUPERGROUP_ID} with user {TEST_USER_ID} ---")
            await run_test(ban_user, "Ban test user from supergroup", chat_id=TEST_SUPERGROUP_ID, user_id=TEST_USER_ID)
            await asyncio.sleep(2)
            await run_test(get_banned_users, "Get banned users (check test user)", chat_id=TEST_SUPERGROUP_ID)
            await run_test(unban_user, "Unban test user from supergroup", chat_id=TEST_SUPERGROUP_ID, user_id=TEST_USER_ID)
            await asyncio.sleep(2)
            # Ensure user is not already participant before inviting
            try:
                 # Use a more specific filter if possible
                 # participants = await client.get_participants(TEST_SUPERGROUP_ID, filter=types.ChannelParticipantsSearch(q=str(TEST_USER_ID)), limit=1)
                 # Simpler check: iterate briefly
                 user_in_group = False
                 async for p in client.iter_participants(TEST_SUPERGROUP_ID, limit=50): # Limit search scope
                     if p.id == TEST_USER_ID:
                          user_in_group = True
                          break
                 if user_in_group:
                      logger.info(f"User {TEST_USER_ID} already in group {TEST_SUPERGROUP_ID}, skipping invite.")
                 else:
                     await run_test(invite_to_group, "Invite test user to supergroup", group_id=TEST_SUPERGROUP_ID, user_ids=[TEST_USER_ID])
                     await asyncio.sleep(2)
            except UserNotParticipantError:
                 # This error is expected if user is not participant, proceed with invite
                 await run_test(invite_to_group, "Invite test user to supergroup (UserNotParticipantError caught)", group_id=TEST_SUPERGROUP_ID, user_ids=[TEST_USER_ID])
                 await asyncio.sleep(2)
            except Exception as p_err:
                 logger.warning(f"Could not check participant status before invite: {p_err}. Attempting invite anyway.")
                 # Try inviting anyway
                 await run_test(invite_to_group, "Invite test user to supergroup (attempt)", group_id=TEST_SUPERGROUP_ID, user_ids=[TEST_USER_ID])
                 await asyncio.sleep(2)

            await run_test(promote_admin, "Promote test user to admin", chat_id=TEST_SUPERGROUP_ID, user_id=TEST_USER_ID)
            await asyncio.sleep(2)
            await run_test(get_admins, "Get admins (check test user)", chat_id=TEST_SUPERGROUP_ID)
            await run_test(demote_admin, "Demote test user from admin", chat_id=TEST_SUPERGROUP_ID, user_id=TEST_USER_ID)
            await asyncio.sleep(2)
            # Leave chat test needs careful consideration - don't leave accidentally!
            # logger.warning(f"--- Skipping leave_chat test for TEST_SUPERGROUP_ID: {TEST_SUPERGROUP_ID} ---")
            # await run_test(leave_chat, "Leave test supergroup", chat_id=TEST_SUPERGROUP_ID)

        # Title/Photo Edit
        original_title_res = await run_test(get_chat, "Get supergroup title before edit", chat_id=TEST_SUPERGROUP_ID)
        original_title = "Unknown"
        if "Title:" in str(original_title_res):
            try:
                 original_title = str(original_title_res).split("Title:")[1].split('\n')[0].strip()
                 logger.info(f"Original title found: '{original_title}'")
            except Exception as title_e:
                 logger.warning(f"Could not parse original title: {title_e}")
        
        random_suffix = ''.join(random.choices(string.ascii_lowercase, k=4))
        new_title = f"Test Title {random_suffix}"
        await run_test(edit_chat_title, "Edit supergroup title", chat_id=TEST_SUPERGROUP_ID, title=new_title)
        await asyncio.sleep(2)
        # Restore original title if possible
        if original_title != "Unknown":
            await run_test(edit_chat_title, "Restore supergroup title", chat_id=TEST_SUPERGROUP_ID, title=original_title)
        else: 
             logger.warning("Could not determine original title to restore.")

        if os.path.exists(TEST_PHOTO_PATH):
             await run_test(edit_chat_photo, "Edit supergroup photo", chat_id=TEST_SUPERGROUP_ID, file_path=TEST_PHOTO_PATH)
             await asyncio.sleep(2)
             await run_test(delete_chat_photo, "Delete supergroup photo", chat_id=TEST_SUPERGROUP_ID)

    # --- Profile & Privacy (Use with EXTREME caution!) ---
    logger.warning("--- Running Profile & Privacy Tests (Potentially Invasive - Mostly Skipped) ---")
    # logger.warning("--- update_profile tests are commented out by default ---")
    # original_bio = "" # Need to fetch current bio first if we want to restore
    # await run_test(update_profile, "Update profile bio", about=f"MCP Test Bio {random.randint(100,999)}")
    # await asyncio.sleep(1)
    # await run_test(update_profile, "Restore profile bio", about=original_bio) # Restore to empty or original
    
    # logger.warning("--- set/delete_profile_photo tests are commented out by default ---")
    # if os.path.exists(TEST_PHOTO_PATH):
    #     await run_test(set_profile_photo, "Set profile photo", file_path=TEST_PHOTO_PATH)
    #     await asyncio.sleep(2)
    #     await run_test(delete_profile_photo, "Delete profile photo")

    await run_test(get_privacy_settings, "Get privacy settings (last seen)")
    # set_privacy_settings is complex and risky to test automatically.
    logger.warning("Skipping set_privacy_settings test due to complexity and risk.")
    # Example: Allow only TEST_USER_ID to see last seen (if TEST_USER_ID is set)
    # if TEST_USER_ID:
    #      logger.warning("Testing set_privacy_settings - allowing TEST_USER_ID for last seen")
    #      await run_test(set_privacy_settings, "Set privacy (last seen - allow test user)", key='status_timestamp', allow_users=[TEST_USER_ID])
    #      await asyncio.sleep(2)
    #      logger.warning("Restoring default privacy for last seen")
    #      await run_test(set_privacy_settings, "Restore privacy (last seen - allow all)", key='status_timestamp', allow_users=[]) # Assuming empty means allow all?

    # --- Bot Operations (Requires TEST_BOT_USERNAME) ---
    if TEST_BOT_USERNAME:
        logger.info(f"--- Running Bot Operations Tests (Bot: {TEST_BOT_USERNAME}) ---")
        await run_test(get_bot_info, "Get bot info", bot_username=TEST_BOT_USERNAME)
        
        # Check if our client is a bot before testing command setting
        is_bot = False
        try:
            me = await client.get_me()
            is_bot = getattr(me, 'bot', False)
        except Exception as e:
            logger.error(f"Error checking if client is a bot: {e}")
        
        if is_bot:
            # Only proceed with set_bot_commands test if we're a bot
            logger.info("Client is a bot account, testing set_bot_commands")
            await run_test(set_bot_commands, "Set bot commands", bot_username=TEST_BOT_USERNAME, 
                           commands=[{'command': 'mcp_test', 'description': 'MCP Test Command'}])
            await asyncio.sleep(2)
            await run_test(set_bot_commands, "Clear bot commands", bot_username=TEST_BOT_USERNAME, commands=[])
        else:
            # Skip the set_bot_commands test if we're not a bot
            logger.warning("Client is a regular user account, not a bot. Skipping set_bot_commands test.")
            logger.info("Note: The set_bot_commands function can only be used by bot accounts.")
    else:
         logger.warning("TEST_BOT_USERNAME not set. Skipping bot tests.")

    # --- Other Operations ---
    logger.info("--- Running Other Operations Tests ---")
    await run_test(search_public_chats, "Search public chats for 'bot'", query="bot")
    await run_test(get_sticker_sets, "Get sticker sets")
    
    # Final check for remaining tools that haven't been explicitly tested
    logger.info("--- Testing Remaining Tools ---")
    
    # Test the archive/unarchive chat functions if not already tested
    if TEST_CHAT_ID:
        try:
            # Only run if we haven't tested these already
            await run_test(archive_chat, "Archive test chat (final check)", chat_id=TEST_CHAT_ID)
            await asyncio.sleep(1)
            await run_test(unarchive_chat, "Unarchive test chat (final check)", chat_id=TEST_CHAT_ID)
        except Exception as e:
            logger.warning(f"Archive/unarchive test failed: {e}")
    
    logger.info("--- All Tests Completed ---")


if __name__ == "__main__":
    nest_asyncio.apply()

    async def main():
        try:
            logger.info("Starting Telegram client for testing...")
            # Ensure client is started and authorized
            await client.start()
            if not await client.is_user_authorized():
                 logger.error("Client authorization failed. Please run main.py interactively first.")
                 sys.exit(1)

            await run_all_tests()

        except Exception as e:
            logger.critical(f"Critical error during test execution: {e}", exc_info=True)
            sys.exit(1)
        finally:
            if client.is_connected():
                logger.info("Disconnecting Telegram client...")
                await client.disconnect()

    asyncio.run(main()) 