import os
import json
import time
import asyncio
import logging

import boto3
from botocore.exceptions import ClientError, ThrottlingException

from ..agent.intent_parser import Intent

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE = 1
BACKOFF_MAX = 8
OPTIMISTIC_LOCK_RETRIES = 3

PROD_BLOCKLIST = {"prod", "production"}


def _contains_prod(path: str) -> bool:
    lowered = path.lower()
    for term in PROD_BLOCKLIST:
        if term in lowered:
            return True
    return False


class SecretsManagerClient:
    def __init__(self):
        self.client = boto3.client("secretsmanager")
        self.auto_create = os.environ.get("AUTO_CREATE_SECRET", "true").lower() == "true"
        self._discovered_secrets: list[dict] | None = None

    def _retry_sync(self, func, *args, **kwargs):
        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except ThrottlingException as e:
                last_exc = e
                wait = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_MAX)
                logger.warning("SM throttled on attempt %d, retrying in %ds: %s", attempt + 1, wait, e)
                time.sleep(wait)
            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code in ("InternalServiceError", "ServiceUnavailable"):
                    last_exc = e
                    wait = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_MAX)
                    logger.warning("SM service error on attempt %d, retrying in %ds: %s", attempt + 1, wait, e)
                    time.sleep(wait)
                else:
                    raise
        raise last_exc

    def list_secrets(self) -> list[dict]:
        all_secrets = []
        try:
            paginator = self.client.get_paginator("list_secrets")
            for page in paginator.paginate():
                for secret in page.get("SecretList", []):
                    name = secret["Name"]
                    if _contains_prod(name):
                        logger.debug("Skipping prod secret: %s", name)
                        continue
                    all_secrets.append({
                        "name": name,
                        "arn": secret.get("ARN", ""),
                        "description": secret.get("Description", ""),
                    })
        except ClientError as e:
            logger.error("Failed to list secrets from AWS SM: %s", e)
        return all_secrets

    def refresh_cache(self):
        self._discovered_secrets = None

    def discover_secret_names(self) -> list[dict]:
        if self._discovered_secrets is not None:
            return self._discovered_secrets

        self._discovered_secrets = self.list_secrets()

        for secret in self._discovered_secrets:
            logger.debug("Discovered secret: %s", secret["name"])

        logger.info("Discovered %d secrets from AWS SM", len(self._discovered_secrets))
        return self._discovered_secrets

    def get_secret_with_version(self, secret_path: str) -> tuple[dict, str]:
        if _contains_prod(secret_path):
            raise PermissionError(f"Access denied: secret path contains 'prod' or 'production': {secret_path}")

        try:
            response = self._retry_sync(self.client.get_secret_value, SecretId=secret_path)
            version_id = response.get("VersionId", "")
            return json.loads(response["SecretString"]), version_id
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                if self.auto_create:
                    logger.info("Secret %s not found — auto-creating", secret_path)
                    resp = self._retry_sync(
                        self.client.create_secret,
                        Name=secret_path,
                        SecretString="{}",
                    )
                    return {}, resp.get("VersionId", "")
                raise
            raise

    def get_secret(self, secret_path: str) -> dict:
        data, _ = self.get_secret_with_version(secret_path)
        return data

    def put_secret_safe(self, secret_path: str, data: dict, expected_version: str) -> str:
        if _contains_prod(secret_path):
            raise PermissionError(f"Access denied: secret path contains 'prod' or 'production': {secret_path}")

        kwargs = {
            "SecretId": secret_path,
            "SecretString": json.dumps(data),
        }
        if expected_version:
            kwargs["ClientRequestToken"] = expected_version

        response = self._retry_sync(self.client.put_secret_value, **kwargs)
        return response.get("VersionId", "")

    async def execute_operation(self, intent: Intent) -> dict:
        secret_path = intent.app_name

        if _contains_prod(secret_path):
            return {
                "status": "error",
                "message": "Operation blocked: secret path contains 'prod' or 'production'.",
                "secret_path": secret_path,
            }

        for lock_attempt in range(OPTIMISTIC_LOCK_RETRIES):
            current_data, version_id = await asyncio.to_thread(
                self.get_secret_with_version, secret_path
            )
            initial_data = dict(current_data)

            if intent.operation == "add":
                if intent.key in current_data:
                    return {
                        "status": "conflict",
                        "message": f"Key '{intent.key}' already exists. Reply 'replace' to overwrite or 'cancel' to abort.",
                        "secret_path": secret_path,
                    }
                current_data[intent.key] = intent.value

            elif intent.operation in ("update", "replace", "append"):
                current_data[intent.key] = intent.value

            elif intent.operation == "rename_key":
                if intent.key not in current_data:
                    return {
                        "status": "error",
                        "message": f"Key '{intent.key}' not found in {secret_path}",
                        "secret_path": secret_path,
                    }
                current_data[intent.new_key] = current_data.pop(intent.key)

            elif intent.operation == "delete_key":
                if intent.key not in current_data:
                    return {
                        "status": "skipped",
                        "message": f"Key '{intent.key}' not found — nothing deleted",
                        "secret_path": secret_path,
                    }
                del current_data[intent.key]

            if current_data == initial_data and intent.operation not in ("add", "delete_key", "rename_key"):
                if intent.operation in ("update", "replace"):
                    return {
                        "status": "success",
                        "secret_path": secret_path,
                        "version_id": version_id,
                    }

            try:
                new_version = await asyncio.to_thread(
                    self.put_secret_safe, secret_path, current_data, version_id
                )
                self._discovered_secrets = None
                return {
                    "status": "success",
                    "secret_path": secret_path,
                    "version_id": new_version,
                }
            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code in ("InvalidRequestException", "PreconditionFailed"):
                    if lock_attempt < OPTIMISTIC_LOCK_RETRIES - 1:
                        wait = 0.1 * (2 ** lock_attempt)
                        logger.warning(
                            "Concurrent modification on %s, retry %d/%d after %0.2fs",
                            secret_path, lock_attempt + 1, OPTIMISTIC_LOCK_RETRIES, wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    else:
                        return {
                            "status": "error",
                            "message": (
                                f"Too many concurrent updates on {secret_path}. "
                                f"Your change to '{intent.key}' could not be applied. Please try again."
                            ),
                            "secret_path": secret_path,
                        }
                raise

        return {
            "status": "error",
            "message": f"Could not apply changes to {secret_path} after {OPTIMISTIC_LOCK_RETRIES} attempts.",
            "secret_path": secret_path,
        }