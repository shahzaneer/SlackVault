import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.slack.responder import SlackResponder
from src.agent.intent_parser import Intent
from src.registry.app_registry import AppRegistry


@pytest.fixture
def mock_responder():
    with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test-token"}):
        responder = SlackResponder(app_registry=None)
    return responder


@pytest.fixture
def mock_responder_with_registry():
    with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test-token"}):
        config = {
            "apps": [
                {"aliases": ["payments", "payments-service"], "secret_path": "slackvault/{environment}/payments-service"},
                {"aliases": ["auth", "auth-service"], "secret_path": "slackvault/{environment}/auth-service"},
            ]
        }
        import tempfile, yaml
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config, f)
            f.flush()
            f_path = f.name
        registry = AppRegistry(config_path=f_path)
        responder = SlackResponder(app_registry=registry)
        os.unlink(f_path)
    return responder


@pytest.mark.asyncio
async def test_reply_success(mock_responder):
    intent = Intent(
        app_name="payments-service",
        environment="stage",
        operation="replace",
        key="DB_PASSWORD",
        value="newpass123",
    )
    mock_responder._post_message = AsyncMock()
    await mock_responder.reply_success(intent, "slackvault/stage/payments-service", "C001", "1234567890.123456", "john.doe")
    mock_responder._post_message.assert_awaited_once()
    msg = mock_responder._post_message.call_args[1]["text"] if "text" in mock_responder._post_message.call_args[1] else mock_responder._post_message.call_args[0][1]
    call_args = mock_responder._post_message.call_args
    assert "payments-service" in call_args[0][0] or "payments-service" in str(call_args)


@pytest.mark.asyncio
async def test_reply_rejection(mock_responder):
    mock_responder._post_message = AsyncMock()
    await mock_responder.reply_rejection("Production secrets are not managed by SlackVault.", "C001", "1234")
    mock_responder._post_message.assert_awaited_once()
    call_args = mock_responder._post_message.call_args[0]
    assert "Rejected" in call_args[0]


@pytest.mark.asyncio
async def test_reply_clarification_without_registry(mock_responder):
    mock_responder._post_message = AsyncMock()
    await mock_responder.reply_clarification("Which app?", "C001", "1234")
    mock_responder._post_message.assert_awaited_once()
    call_args = mock_responder._post_message.call_args[0]
    assert "Which app?" in call_args[0]
    assert "Unknown apps" in call_args[0]


@pytest.mark.asyncio
async def test_reply_clarification_with_registry(mock_responder_with_registry):
    mock_responder_with_registry._post_message = AsyncMock()
    await mock_responder_with_registry.reply_clarification("Which app?", "C001", "1234")
    mock_responder_with_registry._post_message.assert_awaited_once()
    call_args = mock_responder_with_registry._post_message.call_args[0]
    assert "Which app?" in call_args[0]
    assert "slackvault" in call_args[0]


@pytest.mark.asyncio
async def test_reply_conflict(mock_responder):
    mock_responder._post_message = AsyncMock()
    await mock_responder.reply_conflict("Key already exists", "C001", "1234")
    mock_responder._post_message.assert_awaited_once()
    call_args = mock_responder._post_message.call_args[0]
    assert "already exists" in call_args[0]


@pytest.mark.asyncio
async def test_reply_error(mock_responder):
    mock_responder._post_message = AsyncMock()
    await mock_responder.reply_error("Something went wrong", "C001", "1234")
    mock_responder._post_message.assert_awaited_once()
    call_args = mock_responder._post_message.call_args[0]
    assert "Something went wrong" in call_args[0]


@pytest.mark.asyncio
async def test_resolve_username_success(mock_responder):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "user": {"display_name": "john.doe", "real_name": "John Doe", "name": "john"},
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=mock_response)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock()
        MockClient.return_value = instance

        name = await mock_responder.resolve_username("U001")
        assert name == "john.doe"


@pytest.mark.asyncio
async def test_resolve_username_failure(mock_responder):
    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=Exception("Network error"))
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock()
        MockClient.return_value = instance

        name = await mock_responder.resolve_username("U001")
        assert name is None