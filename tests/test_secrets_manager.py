import pytest
from unittest.mock import patch, MagicMock

from src.aws.secrets_manager import SecretsManagerClient
from src.agent.intent_parser import Intent


@pytest.fixture
def sm_client():
    with patch("src.aws.secrets_manager.boto3.client") as mock_boto:
        mock_sm = MagicMock()
        mock_boto.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {
            "SecretString": '{"EXISTING_KEY": "old_value"}'
        }
        mock_sm.put_secret_value.return_value = {"VersionId": "v1"}
        client = SecretsManagerClient()
        client.auto_create = False
        yield client


@pytest.mark.asyncio
async def test_add_operation(sm_client):
    intent = Intent(
        app_name="test-app",
        environment="dev",
        operation="add",
        key="NEW_KEY",
        value="new_value",
    )
    result = await sm_client.execute_operation(intent)
    assert result["status"] == "success"
    assert "test-app" in result["secret_path"]


@pytest.mark.asyncio
async def test_add_existing_key_conflict(sm_client):
    intent = Intent(
        app_name="test-app",
        environment="dev",
        operation="add",
        key="EXISTING_KEY",
        value="new_value",
    )
    result = await sm_client.execute_operation(intent)
    assert result["status"] == "conflict"


@pytest.mark.asyncio
async def test_update_operation(sm_client):
    intent = Intent(
        app_name="test-app",
        environment="dev",
        operation="replace",
        key="EXISTING_KEY",
        value="updated_value",
    )
    result = await sm_client.execute_operation(intent)
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_rename_key(sm_client):
    intent = Intent(
        app_name="test-app",
        environment="dev",
        operation="rename_key",
        key="EXISTING_KEY",
        new_key="RENAMED_KEY",
    )
    result = await sm_client.execute_operation(intent)
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_rename_nonexistent_key(sm_client):
    intent = Intent(
        app_name="test-app",
        environment="dev",
        operation="rename_key",
        key="NONEXISTENT",
        new_key="NEW_KEY",
    )
    result = await sm_client.execute_operation(intent)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_delete_key(sm_client):
    intent = Intent(
        app_name="test-app",
        environment="dev",
        operation="delete_key",
        key="EXISTING_KEY",
    )
    result = await sm_client.execute_operation(intent)
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_delete_nonexistent_key(sm_client):
    intent = Intent(
        app_name="test-app",
        environment="dev",
        operation="delete_key",
        key="NONEXISTENT",
    )
    result = await sm_client.execute_operation(intent)
    assert result["status"] == "skipped"
