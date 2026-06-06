# SlackVault Architecture

## Overview

SlackVault is an AI-powered agent that listens to a Slack channel, understands natural-language secret update requests via DeepSeek LLM, and autonomously applies operations on AWS Secrets Manager with full audit logging to MongoDB.

**Single database: MongoDB** — all audit records are stored in the `slackvault_audit` collection. 

---

## System Architecture

```
Slack Message
  → ALB (HTTPS endpoint)
  → SlackVault Pod (FastAPI)
      → ConcurrencyLimiter (asyncio.Semaphore throttles concurrent processing)
      → SlackHandler (signature verify, event parse, MongoDB+in-memory dedup)
      → Strip @mention prefix (mention mode)
      → ConversationStore: check if this is a confirmation response
      → IntentParser (DeepSeek LLM extracts structured intent)
      → IntentValidator (env guard, app resolution, field checks)
      → AppRegistry (resolve app alias → SM path, substitute {environment})
      → Store pending confirmation → reply with confirmation prompt
      ← User replies "yes" →
      → SecretLockManager (per-secret asyncio.Lock serializes same-secret writes)
      → SecretsManagerClient (read→modify→write with optimistic locking via ClientRequestToken)
      → MongoAuditLogger (write audit to MongoDB)
      → SlackResponder (thread reply back to Slack)
```

---

## Configuration Loading

### Production Mode

Only `APP_SECRET_ARN` is injected via the Kubernetes Deployment manifest. At pod startup:

1. `python-dotenv` loads `.env` (if present, non-overwriting existing env vars)
2. Since `APP_SECRET_ARN` is set → `boto3.get_secret_value()` fetches all config from AWS Secrets Manager
3. All values from the SM secret override/set environment variables
4. IRSA provides the IAM permission to call `GetSecretValue`

### Local Development Mode

When `APP_SECRET_ARN` is not set:

1. `python-dotenv` loads `.env`
2. All config comes from `.env` values directly
3. No AWS credentials needed (mock/local SM for testing)

The `.env.example` file documents all available config variables with comments explaining production vs local usage.

---

## Data Flow

1. Slack sends HTTP POST to `/slack/events`
2. `SlackHandler` verifies HMAC-SHA256 signature
3. URL verification challenges are handled inline
4. Bot messages and duplicates are filtered out
5. In `mention` mode, messages without `@SlackVault` are skipped
6. Bot user ID is resolved at startup via `auth.test` Slack API call
7. `@SlackVault` mention prefix is stripped from message text before LLM parse
8. `IntentParser` sends the cleaned text to DeepSeek LLM with system prompt + few-shot examples
9. LLM returns structured JSON intent (or flags as irrelevant/rejected/needs_clarification)
10. On malformed JSON: retry once with stricter prompt; on second failure → clarification reply
11. `IntentValidator` runs deterministic rules (env must be dev/stage, app must resolve, required fields present)
12. `AppRegistry` resolves the app name alias to a secret path template, then `{environment}` is substituted to produce the final path (e.g., `slackvault/stage/payments-service`)
13. `SecretsManagerClient` executes the operation on AWS SM via boto3 with exponential backoff retry (3 retries, max 8s) on throttling or service errors
14. `MongoAuditLogger` writes the outcome to MongoDB
15. `SlackResponder` posts a threaded reply in Slack (success/rejection/clarification/conflict/error)

---

## Module Reference

### `src/main.py` — FastAPI Entry Point

- Defines `/healthz`, `/readyz`, `/slack/events`, `/stats` endpoints
- `lifespan` context manager: loads config, initializes all components, resolves bot user ID, connects to MongoDB, wires event dedup DB backend
- `/readyz` performs real health checks: MongoDB ping + AWS SM reachability
- Routes incoming Slack events through the full pipeline with concurrency limiting

### `src/lock_manager.py` — Concurrency Control

- `SecretLockManager`: per-secret-path asyncio.Lock that serializes writes to the same secret while allowing different secrets to run in parallel
- `ConcurrencyLimiter`: asyncio.Semaphore that throttles total concurrent event processing (configured via `MAX_CONCURRENT_OPS`, default 10)
- `GET /stats` exposes live metrics: lock count, waiting count, available semaphore slots

### `src/conversation.py` — Confirmation State

- Dual-layer store (in-memory + MongoDB) for pending operation confirmations
- `PendingConfirmation` scoped to user + thread with 10-minute TTL
- Detects confirmation/cancellation responses in thread replies
- Survives pod restarts via MongoDB `slackvault_pending` collection

### `src/startup.py` — Configuration Loader

Two modes:
- **Production**: `APP_SECRET_ARN` is set → calls `boto3.get_secret_value()` and loads all config into `os.environ`
- **Local dev**: `APP_SECRET_ARN` is empty → loads from `.env` file via `python-dotenv`, uses env vars directly

### `src/slack_handler.py` — Event Router

- HMAC-SHA256 signature verification with 5-minute replay window
- URL verification challenge handling
- Event parsing with channel filtering and bot message exclusion
- In-memory deduplication on `event_id`
- Mention mode support

### `src/agent/llm_client.py` — DeepSeek API Client

- Async HTTP client (`httpx`) calling DeepSeek's `/v1/chat/completions`
- Configured via `DEEPSEEK_API_KEY`, `DEEPSEEK_API_BASE_URL`, `DEEPSEEK_MODEL`

### `src/agent/intent_parser.py` — LLM Intent Extraction

- Sends system prompt + user message to DeepSeek
- Parses JSON response into `Intent` dataclass
- Retries once with stricter prompt on JSON parse failure
- Falls back to clarification question on second failure

### `src/agent/intent_validator.py` — Deterministic Validation

Rules:
- `environment` must be `dev` or `stage`
- `app_name` must be present
- `operation` must be one of: `add`, `update`, `replace`, `append`, `rename_key`, `delete_key`
- `key` must be present
- `update`/`replace`/`add`/`append` require `value`
- `rename_key` requires `new_key`

### `src/aws/secrets_manager.py` — AWS SM Operations

- `get_secret_with_version()` — fetches JSON blob + `VersionId` for optimistic locking
- `get_secret()` — fetches JSON blob; auto-creates empty secret if `AUTO_CREATE_SECRET=true`
- `put_secret_safe()` — writes with `ClientRequestToken` (version check); AWS rejects if concurrent modification
- `list_secrets()` — paginated discovery of all secrets under the configured prefix
- `execute_operation()` — handles all 6 operations with:
  - Conflict detection for `add` on existing key
  - Optimistic locking: read→modify→write with retry on `PreconditionFailed` (3 retries)
- Built-in retry with exponential backoff (3 retries, max 8s) on `ThrottlingException` and service errors

### `src/db/base.py` — AuditLogger ABC

Abstract base class defining `connect()`, `log()`, `close()` interface.

### `src/db/mongo.py` — MongoDB Database Layer

- Connects via `motor` (async driver) using `DB_URL` and `MONGO_DB_NAME`
- Manages 3 collections:
  - `slackvault_audit` — audit log with auto-created indexes
  - `slackvault_events` — event dedup with 1-hour TTL index
  - `slackvault_pending` — pending confirmations with 10-min TTL index
- Auto-creates indexes on connect
- Gracefully handles connection failures (logs warning, continues without DB)

### `src/registry/app_registry.py` — App Name Resolver

- Loads aliases from `config/app-registry.yaml`
- Resolves app names using exact match, then fuzzy Levenshtein (distance ≤ 2)
- Returns secret path template with `{environment}` placeholder
- `main.py` substitutes `{environment}` at runtime

### `src/slack/responder.py` — Slack Reply Formatting

- Posts threaded replies via Slack's `chat.postMessage` API
- Methods: `reply_success`, `reply_rejection`, `reply_clarification`, `reply_conflict`, `reply_error`
- `resolve_username()` calls Slack's `users.info` API
- `AppRegistry` is injected to dynamically list known apps in clarification messages

---

## Health Endpoints

| Endpoint | Purpose | Behavior |
|---|---|---|
| `GET /healthz` | Liveness | Returns `{"status": "ok"}` always |
| `GET /readyz` | Readiness | Pings MongoDB + checks AWS SM reachability. Returns 200 with full checks, 503 if degraded |
| `GET /stats` | Concurrency | Returns pending confirmations, active locks, waiting locks, available semaphore slots |

---

## MongoDB Schema

**Collection:** `slackvault_audit`

```json
{
  "_id":            "uuid",
  "created_at":     "ISO8601",
  "slack_user_id":  "U012AB3CD",
  "slack_user_name":"john.doe",
  "channel_id":     "C012XY",
  "message_ts":     "1234567890.123456",
  "app_name":       "payments-service",
  "environment":    "stage",
  "operation":      "replace",
  "secret_path":    "slackvault/stage/payments-service",
  "key_name":       "DB_PASSWORD",
  "status":         "success",
  "error_message":  null,
  "sm_version_id":  "abc123"
}
```

**Auto-created indexes (on startup):**
- `{ app_name: 1, environment: 1 }` — `idx_audit_app_env`
- `{ slack_user_id: 1 }` — `idx_audit_user`
- `{ created_at: -1 }` — `idx_audit_time`

**Collection: `slackvault_events`** (event dedup, 1-hour TTL)

```json
{
  "_id": "evt_001",
  "created_at": "ISO8601"
}
```

**Collection: `slackvault_pending`** (pending confirmations, 10-min TTL)

```json
{
  "_id": "thread-msg-ts",
  "secret_path": "slackvault/dev/payments-service",
  "channel_id": "C001",
  "thread_ts": "123456.789",
  "slack_user_id": "U001",
  "intent": { ... },
  "created_at": "ISO8601",
  "expires_at": "ISO8601"
}
```

---

## Configuration

All config is loaded at startup via `src/startup.py`:

| Environment Variable | Source | Description |
|---|---|---|
| `APP_SECRET_ARN` | K8s manifest / .env | ARN of AWS SM secret containing all other config (production mode) |
| `SLACK_SIGNING_SECRET` | AWS SM / .env | Slack app signing secret |
| `SLACK_BOT_TOKEN` | AWS SM / .env | Slack bot OAuth token |
| `DEEPSEEK_API_KEY` | AWS SM / .env | DeepSeek API key |
| `DEEPSEEK_API_BASE_URL` | AWS SM / .env | DeepSeek API base URL |
| `DEEPSEEK_MODEL` | AWS SM / .env | Model name (default: `deepseek-chat`) |
| `ALLOWED_CHANNEL_IDS` | AWS SM / .env | Comma-separated Slack channel IDs |
| `DB_URL` | AWS SM / .env | MongoDB connection string |
| `MONGO_DB_NAME` | AWS SM / .env | MongoDB database name (default: `slackvault`) |
| `MAX_CONCURRENT_OPS` | AWS SM / .env | Max simultaneous event pipelines (default: `10`) |
| `TRIGGER_MODE` | AWS SM / .env | `mention` or `passive` |
| `AUTO_CREATE_SECRET` | AWS SM / .env | Auto-create missing SM secrets (default: `true`) |
| `LOG_LEVEL` | AWS SM / .env | Python log level (default: `INFO`) |

---

## Local Development

1. Copy `.env.example` to `.env` and fill in values
2. Leave `APP_SECRET_ARN` empty (comment it out) to use local env vars
3. Run `docker-compose up` — starts MongoDB + SlackVault
4. The app reads `.env` via `python-dotenv` at startup

In production, only `APP_SECRET_ARN` is set in the K8s manifest. All other config comes from the AWS SM secret.

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Slack event delivered twice | Deduplicated by `event_id`; second delivery silently dropped |
| LLM returns malformed JSON | Retry once with stricter prompt; on second failure → clarification reply |
| AWS SM throttled | Exponential backoff (3 retries, max 8s); CloudWatch alarm if all retries fail |
| AWS SM service error | Retried with exponential backoff (same as throttling) |
| Concurrent secret modification | Per-secret asyncio Lock + optimistic locking via `ClientRequestToken` with 3 retries |
| Pod restart loses in-memory state | Event dedup + pending confirmations persisted to MongoDB (survive restart) |
| Secret path doesn't exist in SM | Auto-create secret if `AUTO_CREATE_SECRET=true`; reject with explanation if false |
| Production environment requested | Hard reject at validation layer; logged as `status=rejected` |
| Unknown app name | Clarification reply listing registered apps |
| Missing key name | Clarification reply asking for the key |
| DeepSeek API unreachable | Reply "AI service temporarily unavailable" + CloudWatch alarm |
| Bot message in channel | Silently ignored via `bot_id` check |
| DB write fails | Log warning; still reply success to Slack (SM was already updated) |
| Mention mode with @bot prefix | Prefix is stripped before LLM parse |