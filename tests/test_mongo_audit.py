import pytest
import os
from unittest.mock import AsyncMock, MagicMock, patch

from src.agent.intent_parser import Intent
from src.db.mongo import MongoAuditLogger


@pytest.fixture
def intent():
    return Intent(
        app_name="payments-service",
        environment="stage",
        operation="replace",
        key="DB_PASSWORD",
        value="newpass123",
    )


@pytest.fixture
def logger():
    os.environ.setdefault("MONGO_DB_NAME", "slackvault_test")
    return MongoAuditLogger()


@pytest.mark.asyncio
async def test_log_warns_when_not_connected(logger, intent):
    logger._collection = None
    with pytest.raises(Exception):
        pass
    await logger.log(
        intent=intent,
        status="success",
        secret_path="slackvault/stage/payments-service",
        version_id="v1",
        slack_user_id="U001",
        slack_user_name="john.doe",
        channel_id="C001",
        message_ts="1234567890.123456",
    )


@pytest.mark.asyncio
async def test_log_writes_document(logger, intent):
    mock_collection = AsyncMock()
    logger._collection = mock_collection

    await logger.log(
        intent=intent,
        status="success",
        secret_path="slackvault/stage/payments-service",
        version_id="v1",
        slack_user_id="U001",
        slack_user_name="john.doe",
        channel_id="C001",
        message_ts="1234567890.123456",
    )

    mock_collection.insert_one.assert_awaited_once()
    doc = mock_collection.insert_one.call_args[0][0]
    assert doc["app_name"] == "payments-service"
    assert doc["environment"] == "stage"
    assert doc["operation"] == "replace"
    assert doc["key_name"] == "DB_PASSWORD"
    assert doc["status"] == "success"
    assert doc["secret_path"] == "slackvault/stage/payments-service"
    assert doc["slack_user_id"] == "U001"
    assert doc["slack_user_name"] == "john.doe"
    assert doc["channel_id"] == "C001"
    assert doc["message_ts"] == "1234567890.123456"
    assert doc["sm_version_id"] == "v1"


@pytest.mark.asyncio
async def test_log_with_error(logger, intent):
    mock_collection = AsyncMock()
    logger._collection = mock_collection

    await logger.log(
        intent=intent,
        status="failed",
        error_message="Key not found",
        secret_path="slackvault/stage/payments-service",
        slack_user_id="U001",
        channel_id="C001",
        message_ts="1234567890.123456",
    )

    doc = mock_collection.insert_one.call_args[0][0]
    assert doc["status"] == "failed"
    assert doc["error_message"] == "Key not found"


@pytest.mark.asyncio
async def test_close_closes_client():
    os.environ.setdefault("MONGO_DB_NAME", "slackvault_test")
    logger = MongoAuditLogger()
    mock_client = MagicMock()
    logger._client = mock_client

    await logger.close()
    mock_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_close_noop_when_no_client():
    logger = MongoAuditLogger()
    logger._client = None
    await logger.close()


@pytest.mark.asyncio
async def test_connect_skips_when_no_db_url():
    os.environ.pop("DB_URL", None)
    logger = MongoAuditLogger()
    await logger.connect()
    assert logger._collection is None


@pytest.mark.asyncio
async def test_connect_creates_indexes(monkeypatch):
    os.environ["DB_URL"] = "mongodb://localhost:27017"
    os.environ["MONGO_DB_NAME"] = "slackvault_test"

    mock_collection = AsyncMock()
    mock_db = MagicMock()
    mock_db.__getitem__ = MagicMock(return_value=mock_collection)
    mock_client_instance = MagicMock()
    mock_client_instance.__getitem__ = MagicMock(return_value=mock_db)

    module = MagicMock()
    module.AsyncIOMotorClient = MagicMock(return_value=mock_client_instance)
    monkeypatch.setattr("motor.motor_asyncio", module)
    monkeypatch.setattr("src.db.mongo.motor", module)

    logger = MongoAuditLogger()
    await logger.connect()
    assert logger._collection is not None