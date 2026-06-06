import pytest

from src.agent.intent_parser import Intent
from src.agent.intent_validator import IntentValidator


@pytest.fixture
def validator():
    return IntentValidator()


def test_valid_intent(validator):
    intent = Intent(
        app_name="payments-service",
        environment="dev",
        operation="add",
        key="DB_HOST",
        value="localhost",
    )
    result = validator.validate(intent)
    assert result.valid is True


def test_invalid_environment(validator):
    intent = Intent(
        app_name="payments-service",
        environment="prod",
        operation="add",
        key="DB_HOST",
        value="localhost",
    )
    result = validator.validate(intent)
    assert result.valid is False
    assert "not supported" in result.error_message


def test_missing_app_name(validator):
    intent = Intent(
        environment="dev",
        operation="add",
        key="DB_HOST",
        value="localhost",
    )
    result = validator.validate(intent)
    assert result.valid is False
    assert "app" in result.error_message.lower()


def test_invalid_operation(validator):
    intent = Intent(
        app_name="payments-service",
        environment="dev",
        operation="invalid_op",
        key="DB_HOST",
        value="localhost",
    )
    result = validator.validate(intent)
    assert result.valid is False


def test_missing_key(validator):
    intent = Intent(
        app_name="payments-service",
        environment="dev",
        operation="add",
        value="localhost",
    )
    result = validator.validate(intent)
    assert result.valid is False


def test_missing_value_for_update(validator):
    intent = Intent(
        app_name="payments-service",
        environment="dev",
        operation="replace",
        key="DB_HOST",
    )
    result = validator.validate(intent)
    assert result.valid is False


def test_missing_new_key_for_rename(validator):
    intent = Intent(
        app_name="payments-service",
        environment="dev",
        operation="rename_key",
        key="DB_HOST",
    )
    result = validator.validate(intent)
    assert result.valid is False


def test_irrelevant_passes(validator):
    intent = Intent(irrelevant=True)
    result = validator.validate(intent)
    assert result.valid is True


def test_rejected_passes(validator):
    intent = Intent(reject=True, reject_reason="Production not allowed")
    result = validator.validate(intent)
    assert result.valid is True


def test_needs_clarification_passes(validator):
    intent = Intent(needs_clarification=True, clarification_question="Which app?")
    result = validator.validate(intent)
    assert result.valid is True
