import os
import time
import hmac
import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)

INMEMORY_DEDUP_MAX = 10000


class SlackEvent:
    def __init__(
        self,
        event_id: str,
        channel_id: str,
        user_id: str,
        text: str,
        message_ts: str,
        is_bot: bool,
        thread_ts: Optional[str] = None,
    ):
        self.event_id = event_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.text = text
        self.message_ts = message_ts
        self.is_bot = is_bot
        self.thread_ts = thread_ts


class SlackHandler:
    def __init__(self):
        self.signing_secret = os.environ["SLACK_SIGNING_SECRET"]
        self.allowed_channel_ids = set(
            c.strip() for c in os.environ.get("ALLOWED_CHANNEL_IDS", "").split(",") if c.strip()
        )
        self.trigger_mode = os.environ.get("TRIGGER_MODE", "mention")
        self.bot_user_id = None
        self._processed_events: set = set()
        self._db = None

    def set_db(self, db):
        self._db = db

    def verify_signature(self, headers: dict, body: bytes) -> bool:
        timestamp = headers.get("X-Slack-Request-Timestamp")
        signature = headers.get("X-Slack-Signature")

        if not timestamp or not signature:
            return False

        try:
            if abs(time.time() - int(timestamp)) > 300:
                logger.warning("Request timestamp too old — possible replay")
                return False
        except ValueError:
            return False

        sig_base = f"v0:{timestamp}:{body.decode()}"
        computed = "v0=" + hmac.new(
            self.signing_secret.encode(),
            sig_base.encode(),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(computed, signature)

    def handle_url_verification(self, body: dict) -> Optional[str]:
        if body.get("type") == "url_verification":
            return body.get("challenge")
        return None

    def parse_event(self, body: dict) -> Optional[SlackEvent]:
        event = body.get("event", {})
        event_id = body.get("event_id", "")
        channel_id = event.get("channel", "")
        user_id = event.get("user", "")
        text = event.get("text", "")
        message_ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")
        bot_id = event.get("bot_id")
        event_type = event.get("type", "")
        subtype = event.get("subtype", "")

        if event_type != "message":
            return None
        if subtype == "message_changed" or subtype == "message_deleted":
            return None
        if bot_id:
            return None

        if self.allowed_channel_ids and channel_id not in self.allowed_channel_ids:
            logger.info("Ignoring message from non-allowed channel: %s", channel_id)
            return None

        if self.trigger_mode == "mention":
            bot_user_id = self._resolve_bot_user_id()
            if bot_user_id and f"<@{bot_user_id}>" not in text:
                logger.debug("Ignoring non-mention message in mention mode")
                return None

        return SlackEvent(
            event_id=event_id,
            channel_id=channel_id,
            user_id=user_id,
            text=text,
            message_ts=message_ts,
            is_bot=False,
            thread_ts=thread_ts,
        )

    async def is_duplicate(self, event_id: str) -> bool:
        if event_id in self._processed_events:
            return True

        if self._db:
            try:
                if await self._db.is_event_processed(event_id):
                    return True
            except Exception:
                pass

        self._processed_events.add(event_id)

        if self._db:
            try:
                await self._db.mark_event_processed(event_id)
            except Exception:
                pass

        if len(self._processed_events) > INMEMORY_DEDUP_MAX:
            self._processed_events.clear()

        return False

    def set_bot_user_id(self, bot_user_id: str):
        self.bot_user_id = bot_user_id

    def _resolve_bot_user_id(self) -> Optional[str]:
        return self.bot_user_id