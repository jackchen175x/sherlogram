import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# Add the root directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock the telethon client for testing
@pytest.fixture
def mock_client():
    with patch('telethon.TelegramClient') as mock:
        # Create a mock instance of TelegramClient
        client_instance = MagicMock()
        mock.return_value = client_instance
        
        # Mock basic methods
        client_instance.get_dialogs.return_value = []
        client_instance.get_entity.return_value = MagicMock()
        
        yield client_instance

# Test functions
def test_imports():
    """Test that all necessary imports are available"""
    # This will fail if any import is missing
    from telethon import TelegramClient
    from telethon.sessions import StringSession

@pytest.mark.asyncio
async def test_format_entity():
    """Test the format_entity function with different entity types"""
    from main import format_entity
    
    # Test user entity
    user = MagicMock()
    user.id = 123
    user.first_name = "John"
    user.last_name = "Doe"
    user.username = "johndoe"
    user.phone = "+1234567890"
    
    user_result = format_entity(user)
    assert user_result["id"] == 123
    assert user_result["name"] == "John Doe"
    assert user_result["type"] == "user"
    assert user_result["username"] == "johndoe"
    assert user_result["phone"] == "+1234567890"
    
    # Test group entity
    from telethon.tl.types import Chat
    group = MagicMock(spec=Chat)
    group.id = 456
    group.title = "Test Group"
    
    group_result = format_entity(group)
    assert group_result["id"] == 456
    assert group_result["name"] == "Test Group"
    assert group_result["type"] == "group"

@pytest.mark.asyncio
async def test_format_message():
    """Test the format_message function"""
    from main import format_message
    from datetime import datetime
    
    # Create a mock message
    message = MagicMock()
    message.id = 789
    message.date = datetime(2023, 1, 1, 12, 0, 0)
    message.message = "Hello, world!"
    message.from_id = None
    message.media = None
    
    result = format_message(message)
    assert result["id"] == 789
    assert "2023-01-01" in result["date"]
    assert result["text"] == "Hello, world!"
    assert "has_media" not in result

# More tests can be added as the project grows 