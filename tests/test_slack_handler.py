import pytest
import os
import hmac
import hashlib
import time

from src.slack_handler import SlackHandler


@pytest.fixture
def handler():
    os.environ["SLACK_SIGNING_SECRET"] = "test_secret"
    os.environ["ALLOWED_CHANNEL_IDS"] = "C001,C002"
    os.environ["TRIGGER_MODE"] = "passive"
    h = SlackHandler()
    h.set_bot_user_id("B001")
    return h


def test_verify_valid_signature(handler):
    timestamp = str(int(time.time()))
    body = b'{"test": "data"}'
    sig_base = f"v0:{timestamp}:{body.decode()}"
    signature = "v0=" + hmac.new(
        b"test_secret", sig_base.encode(), hashlib.sha256
    ).hexdigest()

    headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature,
    }
    assert handler.verify_signature(headers, body) is True


def test_verify_expired_signature(handler):
    old_timestamp = str(int(time.time()) - 600)
    body = b'{"test": "data"}'
    sig_base = f"v0:{old_timestamp}:{body.decode()}"
    signature = "v0=" + hmac.new(
        b"test_secret", sig_base.encode(), hashlib.sha256
    ).hexdigest()

    headers = {
        "X-Slack-Request-Timestamp": old_timestamp,
        "X-Slack-Signature": signature,
    }
    assert handler.verify_signature(headers, body) is False


def test_url_verification(handler):
    body = {"type": "url_verification", "challenge": "test_challenge"}
    result = handler.handle_url_verification(body)
    assert result == "test_challenge"


def test_parse_message_event(handler):
    body = {
        "event_id": "evt_001",
        "event": {
            "type": "message",
            "channel": "C001",
            "user": "U001",
            "text": "hello world",
            "ts": "1234567890.123456",
        },
    }
    event = handler.parse_event(body)
    assert event is not None
    assert event.event_id == "evt_001"
    assert event.channel_id == "C001"
    assert event.user_id == "U001"
    assert event.text == "hello world"


def test_ignores_bot_messages(handler):
    body = {
        "event_id": "evt_002",
        "event": {
            "type": "message",
            "channel": "C001",
            "user": "U001",
            "text": "bot message",
            "ts": "1234567890.123456",
            "bot_id": "B001",
        },
    }
    event = handler.parse_event(body)
    assert event is None


@pytest.mark.asyncio
async def test_deduplication(handler):
    assert await handler.is_duplicate("evt_001") is False
    assert await handler.is_duplicate("evt_001") is True
