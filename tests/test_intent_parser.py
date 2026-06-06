import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock

from src.agent.intent_parser import IntentParser, Intent
from src.agent.llm_client import DeepSeekClient


@pytest.fixture
def mock_llm_client():
    client = MagicMock(spec=DeepSeekClient)
    client.extract_intent = AsyncMock()
    return client


@pytest.fixture
def parser(mock_llm_client, tmp_path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    prompt_file = prompt_dir / "system_prompt.txt"
    prompt_file.write_text("You are SlackVault. Return JSON.")
    parser_instance = IntentParser(mock_llm_client)
    parser_instance.system_prompt = "You are SlackVault. Return JSON."
    return parser_instance


@pytest.mark.asyncio
async def test_parse_valid_intent(parser, mock_llm_client):
    response_json = json.dumps({
        "irrelevant": False,
        "reject": False,
        "reject_reason": None,
        "needs_clarification": False,
        "clarification_question": None,
        "app_name": "payments-service",
        "environment": "stage",
        "operation": "add",
        "key": "DB_HOST",
        "value": "mydb.internal",
        "new_key": None,
    })
    mock_llm_client.extract_intent.return_value = response_json

    intent = await parser.parse("add DB_HOST=mydb.internal to payments in stage")
    assert intent.app_name == "payments-service"
    assert intent.environment == "stage"
    assert intent.operation == "add"
    assert intent.key == "DB_HOST"
    assert intent.value == "mydb.internal"
    assert intent.irrelevant is False
    assert intent.reject is False


@pytest.mark.asyncio
async def test_parse_irrelevant_message(parser, mock_llm_client):
    response_json = json.dumps({
        "irrelevant": True,
        "reject": False,
        "reject_reason": None,
        "needs_clarification": False,
        "clarification_question": None,
        "app_name": None,
        "environment": None,
        "operation": None,
        "key": None,
        "value": None,
        "new_key": None,
    })
    mock_llm_client.extract_intent.return_value = response_json

    intent = await parser.parse("standup at 10am today everyone")
    assert intent.irrelevant is True


@pytest.mark.asyncio
async def test_parse_production_rejection(parser, mock_llm_client):
    response_json = json.dumps({
        "irrelevant": False,
        "reject": True,
        "reject_reason": "Production secrets are not managed by SlackVault.",
        "needs_clarification": False,
        "clarification_question": None,
        "app_name": "user-service",
        "environment": "prod",
        "operation": "replace",
        "key": "API_KEY",
        "value": "xyz",
        "new_key": None,
    })
    mock_llm_client.extract_intent.return_value = response_json

    intent = await parser.parse("set API_KEY=xyz in prod for user-service")
    assert intent.reject is True
    assert "Production" in intent.reject_reason


@pytest.mark.asyncio
async def test_parse_needs_clarification(parser, mock_llm_client):
    response_json = json.dumps({
        "irrelevant": False,
        "reject": False,
        "reject_reason": None,
        "needs_clarification": True,
        "clarification_question": "Which app should LOG_LEVEL=debug be added to?",
        "app_name": None,
        "environment": "stage",
        "operation": "add",
        "key": "LOG_LEVEL",
        "value": "debug",
        "new_key": None,
    })
    mock_llm_client.extract_intent.return_value = response_json

    intent = await parser.parse("add LOG_LEVEL=debug in stage")
    assert intent.needs_clarification is True
    assert "Which app" in intent.clarification_question


@pytest.mark.asyncio
async def test_parse_malformed_json_retries(parser, mock_llm_client):
    mock_llm_client.extract_intent.side_effect = ["not json at all", json.dumps({
        "irrelevant": True,
        "reject": False,
        "reject_reason": None,
        "needs_clarification": False,
        "clarification_question": None,
        "app_name": None,
        "environment": None,
        "operation": None,
        "key": None,
        "value": None,
        "new_key": None,
    })]

    intent = await parser.parse("random message")
    assert intent.irrelevant is True
    assert mock_llm_client.extract_intent.call_count == 2


@pytest.mark.asyncio
async def test_parse_double_malformed_json_returns_clarification(parser, mock_llm_client):
    mock_llm_client.extract_intent.side_effect = ["not json", "still not json"]

    intent = await parser.parse("broken message")
    assert intent.needs_clarification is True
    assert "rephrase" in intent.clarification_question.lower()


def test_intent_from_dict():
    data = {
        "irrelevant": False,
        "reject": False,
        "reject_reason": None,
        "needs_clarification": False,
        "clarification_question": None,
        "app_name": "auth-service",
        "environment": "dev",
        "operation": "delete_key",
        "key": "CACHE_TTL",
        "value": None,
        "new_key": None,
    }
    intent = Intent.from_dict(data)
    assert intent.app_name == "auth-service"
    assert intent.environment == "dev"
    assert intent.operation == "delete_key"


def test_intent_to_dict():
    intent = Intent(
        app_name="payments-service",
        environment="stage",
        operation="add",
        key="DB_HOST",
        value="mydb.internal",
    )
    d = intent.to_dict()
    assert d["app_name"] == "payments-service"
    assert d["environment"] == "stage"
    assert d["operation"] == "add"