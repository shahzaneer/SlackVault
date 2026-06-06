import time
import logging
from enum import Enum
from typing import Optional, TYPE_CHECKING

from ..agent.intent_parser import Intent

if TYPE_CHECKING:
    from ..db.mongo import MongoAuditLogger

logger = logging.getLogger(__name__)

CONFIRMATION_TTL_SECONDS = 600


class ConfirmationAction(Enum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    NOT_APPLICABLE = "not_applicable"


class ConfirmationResult:
    def __init__(self, action: ConfirmationAction, pending: Optional["PendingConfirmation"] = None):
        self.action = action
        self.pending = pending


class PendingConfirmation:
    def __init__(
        self,
        intent: Intent,
        secret_path: str,
        channel_id: str,
        thread_ts: str,
        slack_user_id: str,
        slack_user_name: Optional[str] = None,
        created_at: Optional[float] = None,
    ):
        self.intent = intent
        self.secret_path = secret_path
        self.channel_id = channel_id
        self.thread_ts = thread_ts
        self.slack_user_id = slack_user_id
        self.slack_user_name = slack_user_name
        self.created_at = created_at or time.time()

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > CONFIRMATION_TTL_SECONDS

    def same_user(self, user_id: str) -> bool:
        return self.slack_user_id == user_id

    def same_thread(self, channel_id: str, thread_ts: str) -> bool:
        return self.channel_id == channel_id and self.thread_ts == thread_ts

    @classmethod
    def from_mongo_doc(cls, doc: dict) -> "PendingConfirmation":
        intent_dict = doc.get("intent", {})
        intent = Intent.from_dict(intent_dict)
        created_at = None
        created_str = doc.get("created_at", "")
        if created_str:
            from datetime import datetime
            try:
                created_at = datetime.fromisoformat(created_str).timestamp()
            except Exception:
                pass
        return cls(
            intent=intent,
            secret_path=doc.get("secret_path", ""),
            channel_id=doc.get("channel_id", ""),
            thread_ts=doc.get("thread_ts", ""),
            slack_user_id=doc.get("slack_user_id", ""),
            slack_user_name=doc.get("slack_user_name"),
            created_at=created_at,
        )


class ConversationStore:
    def __init__(self, db: Optional["MongoAuditLogger"] = None):
        self._db = db
        self._pending: dict[str, PendingConfirmation] = {}

    async def store(self, message_ts: str, confirmation: PendingConfirmation):
        self._evict_expired()
        self._pending[message_ts] = confirmation

        if self._db:
            await self._db.store_pending_confirmation(
                message_ts=message_ts,
                intent=confirmation.intent,
                secret_path=confirmation.secret_path,
                channel_id=confirmation.channel_id,
                thread_ts=confirmation.thread_ts,
                slack_user_id=confirmation.slack_user_id,
                slack_user_name=confirmation.slack_user_name,
            )

        logger.info(
            "Stored pending confirmation for %s in %s (user: %s)",
            confirmation.intent.app_name,
            confirmation.secret_path,
            confirmation.slack_user_id,
        )

    async def check_for_confirmation(
        self, channel_id: str, thread_ts: str, user_id: str, text: str
    ) -> ConfirmationResult:
        self._evict_expired()

        text_lower = text.strip().lower()
        is_confirm = any(w in text_lower for w in ["yes", "confirm", "yep", "yeah", "proceed", "ok", "do it", "y", "replace"])
        is_cancel = any(w in text_lower for w in ["no", "cancel", "abort", "n", "stop", "never mind"])

        if not is_confirm and not is_cancel:
            return ConfirmationResult(ConfirmationAction.NOT_APPLICABLE)

        found_in_memory = None
        for key, pending in list(self._pending.items()):
            if pending.is_expired():
                del self._pending[key]
                if self._db:
                    await self._db.delete_pending_confirmation(key)
                continue

            if not pending.same_thread(channel_id, thread_ts):
                continue

            if not pending.same_user(user_id):
                continue

            found_in_memory = (key, pending)
            break

        if found_in_memory:
            key, pending = found_in_memory
            del self._pending[key]
            if self._db:
                await self._db.delete_pending_confirmation(key)

            if is_confirm:
                return ConfirmationResult(ConfirmationAction.CONFIRMED, pending)
            elif is_cancel:
                return ConfirmationResult(ConfirmationAction.CANCELLED)

        if self._db:
            mongo_doc = await self._db.find_pending_confirmation(channel_id, thread_ts, user_id)
            if mongo_doc:
                pending_from_db = PendingConfirmation.from_mongo_doc(mongo_doc)
                if not pending_from_db.is_expired():
                    if is_confirm:
                        return ConfirmationResult(ConfirmationAction.CONFIRMED, pending_from_db)
                    elif is_cancel:
                        return ConfirmationResult(ConfirmationAction.CANCELLED)

        if is_cancel:
            return ConfirmationResult(ConfirmationAction.CANCELLED)

        return ConfirmationResult(ConfirmationAction.NOT_APPLICABLE)

    def has_pending(self, message_ts: str) -> bool:
        return message_ts in self._pending

    def get_pending(self, message_ts: str) -> Optional[PendingConfirmation]:
        return self._pending.get(message_ts)

    async def remove(self, message_ts: str):
        self._pending.pop(message_ts, None)
        if self._db:
            await self._db.delete_pending_confirmation(message_ts)

    def _evict_expired(self):
        expired = [k for k, v in self._pending.items() if v.is_expired()]
        for k in expired:
            del self._pending[k]

    def pending_count(self) -> int:
        return len(self._pending)