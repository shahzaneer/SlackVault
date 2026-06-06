import os
import json
import logging
from typing import Optional

from .llm_client import DeepSeekClient

logger = logging.getLogger(__name__)


class Intent:
    def __init__(
        self,
        irrelevant: bool = False,
        reject: bool = False,
        reject_reason: Optional[str] = None,
        needs_clarification: bool = False,
        clarification_question: Optional[str] = None,
        confirmation_response: Optional[str] = None,
        app_name: Optional[str] = None,
        environment: Optional[str] = None,
        operation: Optional[str] = None,
        key: Optional[str] = None,
        value: Optional[str] = None,
        new_key: Optional[str] = None,
        raw_message: Optional[str] = None,
    ):
        self.irrelevant = irrelevant
        self.reject = reject
        self.reject_reason = reject_reason
        self.needs_clarification = needs_clarification
        self.clarification_question = clarification_question
        self.confirmation_response = confirmation_response
        self.app_name = app_name
        self.environment = environment
        self.operation = operation
        self.key = key
        self.value = value
        self.new_key = new_key
        self.raw_message = raw_message

    @classmethod
    def from_dict(cls, data: dict) -> "Intent":
        return cls(
            irrelevant=data.get("irrelevant", False),
            reject=data.get("reject", False),
            reject_reason=data.get("reject_reason"),
            needs_clarification=data.get("needs_clarification", False),
            clarification_question=data.get("clarification_question"),
            confirmation_response=data.get("confirmation_response"),
            app_name=data.get("app_name"),
            environment=data.get("environment"),
            operation=data.get("operation"),
            key=data.get("key"),
            value=data.get("value"),
            new_key=data.get("new_key"),
            raw_message=data.get("raw_message"),
        )

    def to_dict(self) -> dict:
        return {
            "irrelevant": self.irrelevant,
            "reject": self.reject,
            "reject_reason": self.reject_reason,
            "needs_clarification": self.needs_clarification,
            "clarification_question": self.clarification_question,
            "confirmation_response": self.confirmation_response,
            "app_name": self.app_name,
            "environment": self.environment,
            "operation": self.operation,
            "key": self.key,
            "value": self.value,
            "new_key": self.new_key,
            "raw_message": self.raw_message,
        }


class IntentParser:
    def __init__(self, llm_client: DeepSeekClient):
        self.llm_client = llm_client
        prompt_path = os.path.join(
            os.path.dirname(__file__), "prompts", "system_prompt.txt"
        )
        with open(prompt_path) as f:
            self.system_prompt = f.read()

    async def parse(self, message: str) -> Intent:
        raw = await self.llm_client.extract_intent(self.system_prompt, message)

        for attempt in range(2):
            try:
                data = json.loads(raw)
                data["raw_message"] = message
                return Intent.from_dict(data)
            except json.JSONDecodeError as e:
                logger.warning("LLM returned malformed JSON (attempt %d): %s", attempt + 1, e)
                if attempt == 0:
                    strict_prompt = (
                        self.system_prompt
                        + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no code fences, no explanation."
                    )
                    raw = await self.llm_client.extract_intent(strict_prompt, message)
                else:
                    return Intent(
                        needs_clarification=True,
                        clarification_question="I had trouble understanding that. Could you rephrase your request?",
                        raw_message=message,
                    )