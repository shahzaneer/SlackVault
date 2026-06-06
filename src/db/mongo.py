import os
import uuid
import time
import logging
from datetime import datetime, timezone
from typing import Optional

from ..agent.intent_parser import Intent
from .base import AuditLogger

logger = logging.getLogger(__name__)

AUDIT_INDEXES = [
    ([("app_name", 1), ("environment", 1)], "idx_audit_app_env"),
    ([("slack_user_id", 1)], "idx_audit_user"),
    ([("created_at", -1)], "idx_audit_time"),
]

PENDING_INDEXES = [
    ([("channel_id", 1), ("thread_ts", 1)], "idx_pending_thread"),
    ([("expires_at", 1)], "idx_pending_expires", {"expireAfterSeconds": 0}),
]

EVENT_TTL_SECONDS = 3600
EVENT_INDEXES = [
    ([("created_at", 1)], "idx_events_ttl", {"expireAfterSeconds": EVENT_TTL_SECONDS}),
]

PENDING_DOC_TTL_SECONDS = 600


class MongoAuditLogger(AuditLogger):
    def __init__(self):
        self.connection_string = os.environ.get("DB_URL", "")
        self.db_name = os.environ.get("MONGO_DB_NAME", "slackvault")
        self._client = None
        self._collection = None
        self._pending_collection = None
        self._events_collection = None

    async def connect(self):
        if not self.connection_string:
            logger.warning("DB_URL not set — audit logging disabled")
            return
        try:
            import motor.motor_asyncio
            self._client = motor.motor_asyncio.AsyncIOMotorClient(self.connection_string)
            db = self._client[self.db_name]
            self._collection = db["slackvault_audit"]
            self._pending_collection = db["slackvault_pending"]
            self._events_collection = db["slackvault_events"]
            await self._create_indexes()
            logger.info("Connected to MongoDB")
        except ImportError:
            logger.error("motor not installed — install with: pip install motor")
        except Exception as e:
            logger.error("Failed to connect to MongoDB: %s", e)

    async def _create_indexes(self):
        if self._collection is None:
            return
        try:
            for keys, name in AUDIT_INDEXES:
                await self._collection.create_index(keys, name=name)
            for keys, name, *extra in PENDING_INDEXES:
                kwargs = extra[0] if extra else {}
                await self._pending_collection.create_index(keys, name=name, **kwargs)
            for keys, name, *extra in EVENT_INDEXES:
                kwargs = extra[0] if extra else {}
                await self._events_collection.create_index(keys, name=name, **kwargs)
            logger.info("Created MongoDB indexes")
        except Exception as e:
            logger.warning("Failed to create MongoDB indexes: %s", e)

    async def log(
        self,
        intent: Intent,
        status: str,
        secret_path: str = "",
        version_id: Optional[str] = None,
        error_message: Optional[str] = None,
        slack_user_id: str = "",
        slack_user_name: Optional[str] = None,
        channel_id: str = "",
        message_ts: str = "",
    ):
        if self._collection is None:
            logger.warning("MongoDB not connected — audit log not written")
            return

        try:
            doc = {
                "_id": str(uuid.uuid4()),
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
                "slack_user_id": slack_user_id,
                "slack_user_name": slack_user_name,
                "channel_id": channel_id,
                "message_ts": message_ts,
                "app_name": intent.app_name or "",
                "environment": intent.environment or "",
                "operation": intent.operation or "",
                "secret_path": secret_path,
                "key_name": intent.key or "",
                "status": status,
                "error_message": error_message,
                "sm_version_id": version_id,
            }
            await self._collection.insert_one(doc)
        except Exception as e:
            logger.error("Failed to write audit log to MongoDB: %s", e)

    async def mark_event_processed(self, event_id: str) -> bool:
        if self._events_collection is None:
            return False
        try:
            await self._events_collection.insert_one({
                "_id": event_id,
                "created_at": datetime.now(tz=timezone.utc),
            })
            return True
        except Exception:
            return False

    async def is_event_processed(self, event_id: str) -> bool:
        if self._events_collection is None:
            return False
        try:
            doc = await self._events_collection.find_one({"_id": event_id})
            return doc is not None
        except Exception:
            return False

    async def store_pending_confirmation(
        self,
        message_ts: str,
        intent: Intent,
        secret_path: str,
        channel_id: str,
        thread_ts: str,
        slack_user_id: str,
        slack_user_name: Optional[str] = None,
    ):
        if self._pending_collection is None:
            return
        try:
            await self._pending_collection.replace_one(
                {"_id": message_ts},
                {
                    "_id": message_ts,
                    "secret_path": secret_path,
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "slack_user_id": slack_user_id,
                    "slack_user_name": slack_user_name,
                    "intent": intent.to_dict(),
                    "created_at": datetime.now(tz=timezone.utc).isoformat(),
                    "expires_at": datetime.now(tz=timezone.utc),
                },
                upsert=True,
            )
        except Exception as e:
            logger.error("Failed to store pending confirmation: %s", e)

    async def find_pending_confirmation(
        self, channel_id: str, thread_ts: str, user_id: str
    ) -> Optional[dict]:
        if self._pending_collection is None:
            return None
        try:
            doc = await self._pending_collection.find_one_and_delete({
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "slack_user_id": user_id,
            })
            return doc
        except Exception as e:
            logger.error("Failed to find pending confirmation: %s", e)
            return None

    async def delete_pending_confirmation(self, message_ts: str):
        if self._pending_collection is None:
            return
        try:
            await self._pending_collection.delete_one({"_id": message_ts})
        except Exception as e:
            logger.error("Failed to delete pending confirmation: %s", e)

    async def pending_count(self) -> int:
        if self._pending_collection is None:
            return 0
        try:
            return await self._pending_collection.count_documents({})
        except Exception:
            return 0

    async def close(self):
        if self._client:
            self._client.close()