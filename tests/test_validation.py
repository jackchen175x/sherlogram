import pytest
import os

os.environ["TELEGRAM_API_ID"] = "12345"
os.environ["TELEGRAM_API_HASH"] = "dummy_hash"
from tg_mcp.common import validate_id


# A simple async function to be decorated for testing
@validate_id("user_id", "chat_id", "user_ids")
async def dummy_function(**kwargs):
    return "success", kwargs


@pytest.mark.asyncio
async def test_valid_integer_id():
    result, kwargs = await dummy_function(user_id=12345)
    assert result == "success"
    assert kwargs["user_id"] == 12345


@pytest.mark.asyncio
async def test_valid_negative_integer_id():
    result, kwargs = await dummy_function(chat_id=-100123456)
    assert result == "success"
    assert kwargs["chat_id"] == -100123456


@pytest.mark.asyncio
async def test_valid_string_integer_id():
    result, kwargs = await dummy_function(user_id="12345")
    assert result == "success"
    assert kwargs["user_id"] == 12345


@pytest.mark.asyncio
async def test_valid_username():
    result, kwargs = await dummy_function(user_id="@test_user")
    assert result == "success"
    assert kwargs["user_id"] == "@test_user"


@pytest.mark.asyncio
async def test_valid_username_without_at():
    result, kwargs = await dummy_function(user_id="test_user_long_enough")
    assert result == "success"
    assert kwargs["user_id"] == "test_user_long_enough"


@pytest.mark.asyncio
async def test_valid_list_of_ids():
    result, kwargs = await dummy_function(user_ids=[123, "456", "@test_user"])
    assert result == "success"
    assert kwargs["user_ids"] == [123, 456, "@test_user"]


# 校验失败以异常抛出（而非 return 字符串），MCP 协议层才会置 isError=True。
# 否则 agent 在批量调查里可能把错误当成一次无害的空结果略过。
@pytest.mark.asyncio
async def test_invalid_float_id():
    with pytest.raises(RuntimeError) as exc:
        await dummy_function(user_id=123.45)
    assert "Invalid user_id" in str(exc.value)
    assert "Type must be an integer or a string" in str(exc.value)


@pytest.mark.asyncio
async def test_invalid_string_id():
    with pytest.raises(RuntimeError) as exc:
        await dummy_function(user_id="inv")  # too short
    assert "Invalid user_id" in str(exc.value)
    assert "Must be a valid integer ID, or a username string" in str(exc.value)


@pytest.mark.asyncio
async def test_integer_out_of_range():
    with pytest.raises(RuntimeError) as exc:
        await dummy_function(user_id=2**64)
    assert "Invalid user_id" in str(exc.value)
    assert "out of the valid integer range" in str(exc.value)


@pytest.mark.asyncio
async def test_invalid_item_in_list():
    with pytest.raises(RuntimeError) as exc:
        await dummy_function(user_ids=[123, "456", 123.45])
    assert "Invalid user_ids" in str(exc.value)
    assert "Type must be an integer or a string" in str(exc.value)


@pytest.mark.asyncio
async def test_no_id_provided():
    result, kwargs = await dummy_function()
    assert result == "success"


@pytest.mark.asyncio
async def test_none_id_provided():
    result, kwargs = await dummy_function(user_id=None)
    assert result == "success"


@pytest.mark.asyncio
async def test_me_is_allowed():
    result, kwargs = await dummy_function(chat_id="me")
    assert result == "success" and kwargs["chat_id"] == "me"
