import os
import json
import logging
import httpx

logger = logging.getLogger(__name__)


class DeepSeekClient:
    def __init__(self):
        self.api_key = os.environ["DEEPSEEK_API_KEY"]
        self.base_url = os.environ.get("DEEPSEEK_API_BASE_URL", "https://api.deepseek.com")
        self.model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    async def chat_completion(self, messages: list[dict], temperature: float = 0.0) -> dict:
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()

    async def extract_intent(self, system_prompt: str, user_message: str) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        try:
            result = await self.chat_completion(messages)
            content = result["choices"][0]["message"]["content"].strip()
            return content
        except httpx.HTTPStatusError as e:
            logger.error("DeepSeek API HTTP error: %s", e)
            raise
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logger.error("DeepSeek API unexpected response: %s", e)
            raise
