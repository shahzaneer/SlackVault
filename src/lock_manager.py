import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class SecretLock:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._waiting = 0

    @property
    def waiting(self) -> int:
        return self._waiting


class SecretLockManager:
    def __init__(self, max_concurrent_secrets: int = 50):
        self._locks: dict[str, SecretLock] = {}
        self._max_concurrent = max_concurrent_secrets

    def acquire(self, secret_path: str) -> SecretLock:
        key = secret_path.lower()
        if key not in self._locks:
            self._locks[key] = SecretLock()
        lock = self._locks[key]
        return lock

    async def execute_locked(
        self, secret_path: str, operation_name: str, callback
    ):
        sl = self.acquire(secret_path)
        sl._waiting += 1

        try:
            async with sl._lock:
                logger.info("Acquired lock for %s (%s)", secret_path, operation_name)
                result = await callback()
                return result
        finally:
            sl._waiting -= 1

    def lock_count(self) -> int:
        return len(self._locks)

    def waiting_count(self) -> int:
        return sum(l.waiting for l in self._locks.values())


class ConcurrencyLimiter:
    def __init__(self, max_concurrent: int = 10):
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def run(self, name: str, callback):
        async with self._semaphore:
            logger.debug("Concurrency slot acquired for %s", name)
            return await callback()

    @property
    def available_slots(self) -> int:
        return self._semaphore._value