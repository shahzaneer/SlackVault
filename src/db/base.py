from abc import ABC, abstractmethod
from typing import Optional

from ..agent.intent_parser import Intent


class AuditLogger(ABC):
    @abstractmethod
    async def connect(self):
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    async def close(self):
        ...
