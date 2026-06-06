import os
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from src.startup import load_config
from src.slack_handler import SlackHandler
from src.agent.llm_client import DeepSeekClient
from src.agent.intent_parser import IntentParser, Intent
from src.agent.intent_validator import IntentValidator
from src.aws.secrets_manager import SecretsManagerClient
from src.db.mongo import MongoAuditLogger
from src.slack.responder import SlackResponder
from src.registry.app_registry import AppRegistry
from src.conversation import ConversationStore, PendingConfirmation, ConfirmationAction
from src.lock_manager import SecretLockManager, ConcurrencyLimiter

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_config()
    app.state.slack_handler = SlackHandler()
    app.state.llm_client = DeepSeekClient()
    app.state.intent_parser = IntentParser(app.state.llm_client)
    app.state.intent_validator = IntentValidator()
    app.state.sm_client = SecretsManagerClient()
    app.state.audit_logger = MongoAuditLogger()
    await app.state.audit_logger.connect()

    app.state.slack_handler.set_db(app.state.audit_logger)

    app.state.app_registry = AppRegistry(sm_client=app.state.sm_client)
    app.state.slack_responder = SlackResponder(app_registry=app.state.app_registry)
    app.state.conversation_store = ConversationStore(db=app.state.audit_logger)
    app.state.lock_manager = SecretLockManager()
    app.state.concurrency_limiter = ConcurrencyLimiter(
        max_concurrent=int(os.environ.get("MAX_CONCURRENT_OPS", "10"))
    )

    logger.info("Discovering secrets from AWS Secrets Manager...")
    discovered = app.state.app_registry.discover_from_aws()
    logger.info("Discovered %d app groups from AWS SM", len(discovered))

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if bot_token:
        bot_user_id = await _resolve_bot_user_id(bot_token)
        if bot_user_id:
            app.state.slack_handler.set_bot_user_id(bot_user_id)
            logger.info("Resolved bot user_id: %s", bot_user_id)

    yield
    await app.state.audit_logger.close()


async def _resolve_bot_user_id(bot_token: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {bot_token}"},
            )
            data = resp.json()
            if data.get("ok"):
                return data.get("user_id")
    except Exception as e:
        logger.warning("Failed to resolve bot user_id: %s", e)
    return None


app = FastAPI(title="SlackVault", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    checks = {"mongodb": False, "secrets_manager": False}

    try:
        if app.state.audit_logger._collection is not None:
            await app.state.audit_logger._collection.database.command("ping")
            checks["mongodb"] = True
    except Exception:
        pass

    try:
        app.state.sm_client.client.list_secrets(MaxResults=1)
        checks["secrets_manager"] = True
    except Exception:
        pass

    status_code = 200 if checks["mongodb"] and checks["secrets_manager"] else 503
    overall = "ok" if status_code == 200 else "degraded"
    return JSONResponse(status_code=status_code, content={"status": overall, "checks": checks})


@app.get("/stats")
async def stats():
    return {
        "pending_confirmations": app.state.conversation_store.pending_count(),
        "active_locks": app.state.lock_manager.lock_count(),
        "waiting_locks": app.state.lock_manager.waiting_count(),
        "available_concurrency_slots": app.state.concurrency_limiter.available_slots,
    }


@app.post("/slack/events")
async def slack_events(request: Request):
    body_bytes = await request.body()
    headers = dict(request.headers)

    slack_handler: SlackHandler = request.app.state.slack_handler
    concurrency_limiter: ConcurrencyLimiter = request.app.state.concurrency_limiter

    if not slack_handler.verify_signature(headers, body_bytes):
        logger.warning("Invalid Slack signature")
        return JSONResponse(status_code=403, content={"error": "Invalid signature"})

    body = await request.json()

    challenge = slack_handler.handle_url_verification(body)
    if challenge:
        return Response(content=challenge, media_type="text/plain")

    event = slack_handler.parse_event(body)
    if not event:
        return Response(status_code=200, content="")

    if await slack_handler.is_duplicate(event.event_id):
        logger.debug("Duplicate event %s — skipping", event.event_id)
        return Response(status_code=200, content="")

    bot_user_id = slack_handler.bot_user_id
    text_clean = event.text
    if bot_user_id:
        text_clean = text_clean.replace(f"<@{bot_user_id}>", "").strip()

    logger.info(
        "Processing event",
        extra={
            "event_id": event.event_id,
            "channel_id": event.channel_id,
            "user_id": event.user_id,
            "text": text_clean,
        },
    )

    return await concurrency_limiter.run(
        f"event_{event.event_id}",
        lambda: _process_event(request, event, text_clean),
    )


async def _process_event(request: Request, event, text_clean: str):
    intent_parser: IntentParser = request.app.state.intent_parser
    intent_validator: IntentValidator = request.app.state.intent_validator
    sm_client: SecretsManagerClient = request.app.state.sm_client
    audit_logger: MongoAuditLogger = request.app.state.audit_logger
    slack_responder: SlackResponder = request.app.state.slack_responder
    app_registry: AppRegistry = request.app.state.app_registry
    conversation_store: ConversationStore = request.app.state.conversation_store
    lock_manager: SecretLockManager = request.app.state.lock_manager

    thread_ts = event.thread_ts or event.message_ts

    confirmation_result = await conversation_store.check_for_confirmation(
        event.channel_id, thread_ts, event.user_id, text_clean
    )

    if confirmation_result.action == ConfirmationAction.CONFIRMED:
        slack_user_name = await slack_responder.resolve_username(event.user_id)
        pending = confirmation_result.pending
        resolved_path = pending.secret_path

        async def _execute():
            intent_for_sm = Intent(
                app_name=resolved_path,
                environment=pending.intent.environment,
                operation=pending.intent.operation,
                key=pending.intent.key,
                value=pending.intent.value,
                new_key=pending.intent.new_key,
            )
            return await sm_client.execute_operation(intent_for_sm)

        result = await lock_manager.execute_locked(resolved_path, pending.intent.operation or "op", _execute)

        if result["status"] == "success":
            await slack_responder.reply_success(
                pending.intent, resolved_path,
                pending.channel_id, pending.thread_ts,
                slack_user_name or pending.slack_user_id,
            )
            await audit_logger.log(
                intent=pending.intent, status="success",
                secret_path=resolved_path, version_id=result.get("version_id"),
                slack_user_id=event.user_id, slack_user_name=slack_user_name,
                channel_id=event.channel_id, message_ts=event.message_ts,
            )
        elif result["status"] in ("conflict", "skipped"):
            await slack_responder.reply_conflict(
                result["message"], pending.channel_id, pending.thread_ts
            )
            await audit_logger.log(
                intent=pending.intent, status=result["status"],
                error_message=result.get("message"),
                slack_user_id=event.user_id, slack_user_name=slack_user_name,
                channel_id=event.channel_id, message_ts=event.message_ts,
                secret_path=resolved_path,
            )
        elif result["status"] == "error":
            await slack_responder.reply_error(
                result["message"], pending.channel_id, pending.thread_ts
            )
            await audit_logger.log(
                intent=pending.intent, status="failed",
                error_message=result.get("message"),
                slack_user_id=event.user_id, slack_user_name=slack_user_name,
                channel_id=event.channel_id, message_ts=event.message_ts,
                secret_path=resolved_path,
            )
        return Response(status_code=200, content="")

    if confirmation_result.action == ConfirmationAction.CANCELLED:
        slack_user_name = await slack_responder.resolve_username(event.user_id)
        await slack_responder.reply_cancelled(event.channel_id, thread_ts)
        await audit_logger.log(
            intent=Intent(app_name="cancelled", environment="cancelled", operation="cancelled", key="cancelled"),
            status="rejected", error_message="User cancelled confirmation",
            slack_user_id=event.user_id, slack_user_name=slack_user_name,
            channel_id=event.channel_id, message_ts=event.message_ts,
        )
        return Response(status_code=200, content="")

    intent = await intent_parser.parse(text_clean)

    slack_user_name = await slack_responder.resolve_username(event.user_id)

    if intent.confirmation_response and intent.irrelevant:
        await slack_responder.reply_error(
            "I don't have a pending operation to confirm. Please send your request again.",
            event.channel_id, thread_ts,
        )
        return Response(status_code=200, content="")

    if intent.irrelevant:
        logger.debug("Event %s classified as irrelevant", event.event_id)
        return Response(status_code=200, content="")

    validation = intent_validator.validate(intent)
    if not validation.valid:
        await audit_logger.log(
            intent=intent, status="rejected", error_message=validation.error_message,
            slack_user_id=event.user_id, slack_user_name=slack_user_name,
            channel_id=event.channel_id, message_ts=event.message_ts,
        )
        await slack_responder.reply_clarification(
            validation.error_message, event.channel_id, event.message_ts
        )
        return Response(status_code=200, content="")

    if intent.reject:
        await audit_logger.log(
            intent=intent, status="rejected", error_message=intent.reject_reason,
            slack_user_id=event.user_id, slack_user_name=slack_user_name,
            channel_id=event.channel_id, message_ts=event.message_ts,
        )
        await slack_responder.reply_rejection(
            intent.reject_reason, event.channel_id, event.message_ts
        )
        return Response(status_code=200, content="")

    if intent.needs_clarification:
        await audit_logger.log(
            intent=intent, status="rejected", error_message=intent.clarification_question,
            slack_user_id=event.user_id, slack_user_name=slack_user_name,
            channel_id=event.channel_id, message_ts=event.message_ts,
        )
        await slack_responder.reply_clarification(
            intent.clarification_question, event.channel_id, event.message_ts
        )
        return Response(status_code=200, content="")

    resolved_path_template = app_registry.resolve(intent.app_name)
    if not resolved_path_template and app_registry.needs_refresh():
        logger.info("App %s not found in cache — refreshing registry", intent.app_name)
        await app_registry.refresh()
        resolved_path_template = app_registry.resolve(intent.app_name)

    if not resolved_path_template:
        known = app_registry.get_known_app_names()
        msg = f"Unknown app '{intent.app_name}'. Known apps: {', '.join(known)}"
        await audit_logger.log(
            intent=intent, status="rejected", error_message=msg,
            slack_user_id=event.user_id, slack_user_name=slack_user_name,
            channel_id=event.channel_id, message_ts=event.message_ts,
        )
        await slack_responder.reply_clarification(msg, event.channel_id, event.message_ts)
        return Response(status_code=200, content="")

    resolved_path = resolved_path_template.replace("{environment}", intent.environment or "")

    pending_confirmation = PendingConfirmation(
        intent=intent,
        secret_path=resolved_path,
        channel_id=event.channel_id,
        thread_ts=thread_ts,
        slack_user_id=event.user_id,
        slack_user_name=slack_user_name,
    )
    await conversation_store.store(event.message_ts, pending_confirmation)

    await slack_responder.reply_confirmation_request(
        intent, resolved_path, event.channel_id, thread_ts,
        slack_user_name or event.user_id,
    )

    return Response(status_code=200, content="")