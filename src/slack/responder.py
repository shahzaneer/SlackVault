import os
import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

import httpx

from ..agent.intent_parser import Intent

if TYPE_CHECKING:
    from ..registry.app_registry import AppRegistry

logger = logging.getLogger(__name__)


class SlackResponder:
    def __init__(self, app_registry: Optional["AppRegistry"] = None):
        self.bot_token = os.environ["SLACK_BOT_TOKEN"]
        self.api_base = "https://slack.com/api"
        self.app_registry = app_registry

    async def _post_message(self, channel: str, text: str, thread_ts: Optional[str] = None) -> dict:
        url = f"{self.api_base}/chat.postMessage"
        headers = {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "channel": channel,
            "text": text,
            "thread_ts": thread_ts,
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                logger.error("Slack API error: %s", data.get("error", "unknown"))
            return data

    async def resolve_username(self, user_id: str) -> Optional[str]:
        url = f"{self.api_base}/users.info"
        headers = {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json",
        }
        params = {"user": user_id}

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()
                data = response.json()
                if data.get("ok"):
                    user = data["user"]
                    return user.get("display_name") or user.get("real_name") or user.get("name")
        except Exception as e:
            logger.warning("Failed to resolve username for %s: %s", user_id, e)

        return None

    async def reply_success(self, intent: Intent, secret_path: str, channel: str, thread_ts: str, slack_user: str):
        text = (
            f"✅ Done!\n"
            f"  App:          {intent.app_name}\n"
            f"  Environment:  {intent.environment}\n"
            f"  Operation:    {intent.operation}\n"
            f"  Key:          {intent.key}\n"
            f"  SM Path:      {secret_path}\n"
            f"  Requested by: @{slack_user}\n"
            f"  Time:         {datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        await self._post_message(channel, text, thread_ts)

    async def reply_rejection(self, reason: str, channel: str, thread_ts: str):
        text = f"🚫 Rejected: {reason}"
        await self._post_message(channel, text, thread_ts)

    async def reply_clarification(self, question: str, channel: str, thread_ts: str):
        known = "Unknown apps - app registry not configured"
        if self.app_registry:
            known = ", ".join(self.app_registry.get_known_apps())
        text = f"🤔 {question}\nKnown apps: {known}"
        await self._post_message(channel, text, thread_ts)

    async def reply_conflict(self, message: str, channel: str, thread_ts: str):
        text = f"⚠️ {message}"
        await self._post_message(channel, text, thread_ts)

    async def reply_error(self, message: str, channel: str, thread_ts: str):
        text = f"❌ {message}"
        await self._post_message(channel, text, thread_ts)

    async def reply_confirmation_request(
        self, intent: Intent, secret_path: str, channel: str, thread_ts: str, slack_user: str
    ):
        op_descriptions = {
            "add": f"Add `{intent.key}` = `{intent.value}`",
            "update": f"Update `{intent.key}` = `{intent.value}`",
            "replace": f"Replace `{intent.key}` = `{intent.value}`",
            "append": f"Append `{intent.key}` = `{intent.value}`",
            "rename_key": f"Rename `{intent.key}` → `{intent.new_key}`",
            "delete_key": f"Delete key `{intent.key}`",
        }
        desc = op_descriptions.get(intent.operation, f"{intent.operation} {intent.key}")
        text = (
            f"🔎 Please confirm:\n"
            f"  {desc}\n"
            f"  App:          {intent.app_name}\n"
            f"  Environment:  {intent.environment}\n"
            f"  Secret:       {secret_path}\n"
            f"  Requested by: @{slack_user}\n\n"
            f"Reply *yes* to confirm or *cancel* to abort. This request expires in 10 minutes."
        )
        return await self._post_message(channel, text, thread_ts)

    async def reply_cancelled(self, channel: str, thread_ts: str):
        text = "🚫 Operation cancelled."
        await self._post_message(channel, text, thread_ts)

    async def reply_confirmation_expired(self, channel: str, thread_ts: str):
        text = "⏰ Confirmation expired. Please send your request again."
        await self._post_message(channel, text, thread_ts)

    async def reply_confirmation_wrong_user(self, channel: str, thread_ts: str, original_user: str):
        text = f"🔒 Only the original requester (<@{original_user}>) can confirm this operation."
        await self._post_message(channel, text, thread_ts)

    async def reply_ambiguous_app(self, app_name: str, candidates: list[str], channel: str, thread_ts: str):
        text = (
            f"🤔 I found multiple apps matching '{app_name}'. Which one did you mean?\n"
            + "\n".join(f"  • {c}" for c in candidates)
            + "\n\nPlease rephrase your request with the exact app name."
        )
        await self._post_message(channel, text, thread_ts)