import logging
from typing import Optional

from .intent_parser import Intent

logger = logging.getLogger(__name__)

VALID_OPERATIONS = {"add", "update", "replace", "append", "rename_key", "delete_key"}
VALID_ENVIRONMENTS = {"dev", "stage"}


class ValidationResult:
    def __init__(self, valid: bool, error_message: Optional[str] = None, intent: Optional[Intent] = None):
        self.valid = valid
        self.error_message = error_message
        self.intent = intent


class IntentValidator:
    def validate(self, intent: Intent) -> ValidationResult:
        if intent.irrelevant:
            return ValidationResult(valid=True, intent=intent)

        if intent.reject:
            return ValidationResult(valid=True, intent=intent)

        if intent.needs_clarification:
            return ValidationResult(valid=True, intent=intent)

        if intent.environment not in VALID_ENVIRONMENTS:
            return ValidationResult(
                valid=False,
                error_message=(
                    f"Environment '{intent.environment}' is not supported. "
                    f"Use one of: {', '.join(VALID_ENVIRONMENTS)}"
                ),
                intent=intent,
            )

        if not intent.app_name:
            return ValidationResult(
                valid=False,
                error_message="I couldn't determine which app this request is for. Known apps are listed in the app registry.",
                intent=intent,
            )

        if intent.operation not in VALID_OPERATIONS:
            return ValidationResult(
                valid=False,
                error_message=f"Operation '{intent.operation}' is not supported.",
                intent=intent,
            )

        if not intent.key:
            return ValidationResult(
                valid=False,
                error_message="Which environment variable key should I operate on?",
                intent=intent,
            )

        if intent.operation in {"update", "replace", "add", "append"} and not intent.value:
            return ValidationResult(
                valid=False,
                error_message=f"What value should I set for {intent.key}?",
                intent=intent,
            )

        if intent.operation == "rename_key" and not intent.new_key:
            return ValidationResult(
                valid=False,
                error_message="What should the new key name be for the rename?",
                intent=intent,
            )

        return ValidationResult(valid=True, intent=intent)
