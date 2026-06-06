import os
import json
import logging

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def load_config():
    load_dotenv(override=False)

    arn = os.environ.get("APP_SECRET_ARN")
    if not arn:
        logger.info("APP_SECRET_ARN not set — using local environment variables")
        return

    load_config_from_sm(arn)


def load_config_from_sm(arn: str):
    try:
        import boto3
    except ImportError:
        logger.error("boto3 not installed — cannot load config from Secrets Manager")
        raise

    client = boto3.client("secretsmanager")
    try:
        response = client.get_secret_value(SecretId=arn)
        secrets = json.loads(response["SecretString"])
    except Exception as e:
        logger.error("Failed to load config from Secrets Manager: %s", e)
        raise

    for key, value in secrets.items():
        os.environ[key] = str(value)

    logger.info("Loaded %d config values from Secrets Manager (ARN: %s)", len(secrets), arn)