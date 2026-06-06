# SlackVault — End-to-End Flow of Execution

## Request Lifecycle

A single Slack message follows this pipeline through the system.

```
Slack -> ALB -> FastAPI(/slack/events) -> SlackHandler.verify_signature
                                        -> SlackHandler.parse_event
                                        -> SlackHandler.is_duplicate
                                        -> ConcurrencyLimiter.run
                                        -> ConversationStore.check_for_confirmation
                                        -> IntentParser.parse (DeepSeek LLM)
                                        -> IntentValidator.validate
                                        -> AppRegistry.resolve
                                        -> PendingConfirmation.store -> SlackResponder.reply_confirmation_request
                                        -> [user replies "yes"]
                                        -> SecretLockManager.execute_locked
                                        -> SecretsManagerClient.execute_operation
                                        -> MongoAuditLogger.log
                                        -> SlackResponder.reply_success
```

Total latency of a successful request-confirm-execute cycle: **5–12 seconds**

---

## Step-by-Step Breakdown

### Step 1: ALB (AWS Application Load Balancer)

| Field | Value |
|---|---|
| **URL** | `https://<alb-hostname>/slack/events` |
| **Method** | `POST` |
| **Protocol** | HTTPS (ACM certificate) |
| **Input** | Raw HTTP POST from Slack (JSON body + `X-Slack-Signature` + `X-Slack-Request-Timestamp`) |
| **Output** | Forwarded to FastAPI pod on port 8000 |
| **Latency** | ~1–2ms (network transit inside AWS) |
| **Notes** | ALB target group routes to ClusterIP service, 3 pod replicas |

---

### Step 2: FastAPI Router

| Route | Input | Output | Latency | Notes |
|---|---|---|---|---|
| `POST /slack/events` | Slack event JSON | `200 OK` (always) | — | Entry point for all Slack messages |
| `GET /healthz` | None | `{"status": "ok"}` | ~1us | liveness probe |
| `GET /readyz` | None | `{"status": "ok"/"degraded", "checks": {...}}` | ~500ms | pings MongoDB + AWS SM |
| `GET /stats` | None | `{"pending_confirmations": int, "active_locks": int, "waiting_locks": int, "available_concurrency_slots": int}` | ~1us | live capacity |

---

### Step 3: Slack Signature Verification

**Function:** `SlackHandler.verify_signature(headers, body_bytes) -> bool`

| Field | Value |
|---|---|
| **Input** | `headers: dict` (with `X-Slack-Request-Timestamp`, `X-Slack-Signature`), `body: bytes` |
| **Output** | `True` (valid) or `False` (invalid), caller returns 403 on invalid |
| **Latency** | ~50us |
| **Logic** | HMAC-SHA256 of `v0:{timestamp}:{body}` using `SLACK_SIGNING_SECRET`; rejects timestamps >300s old |
| **Failure path** | 403 response, logged warning, no further processing |

---

### Step 4: URL Challenge & Event Parsing

**Function:** `SlackHandler.handle_url_verification(body) -> Optional[str]`

| Field | Value |
|---|---|
| **Input** | `body: dict` with `type` field |
| **Output** | `challenge` string if type == `url_verification`, else `None` |
| **Latency** | ~5us |
| **Notes** | Slack sends this once during app setup to verify the endpoint |

**Function:** `SlackHandler.parse_event(body) -> Optional[SlackEvent]`

| Field | Value |
|---|---|
| **Input** | `body: dict` (full Slack event payload) |
| **Output** | `SlackEvent` or `None` (filtered out) |
| **Latency** | ~10us |
| **Filters applied** | `event_type != "message"` (skip) / `subtype in ("message_changed", "message_deleted")` (skip) / `bot_id` present (skip) / channel not in `ALLOWED_CHANNEL_IDS` (skip) / no `@mention` in mention mode (skip) |

---

### Step 5: Event Dedup (Async)

**Method:** `await SlackHandler.is_duplicate(event_id) -> bool`

| Field | Value |
|---|---|
| **Input** | `event_id: str` (Slack's unique event ID) |
| **Output** | `True` (already processed) or `False` (new event) |
| **Best case** | ~5us — hot path, in-memory `set()` lookup |
| **Worst case** | ~50ms — pod just restarted, falls through to MongoDB `slackvault_events` check |
| **Path on miss** | Adds to in-memory set + MongoDB (fire-and-forget) |
| **Storage** | In-memory set (max 10K entries, auto-clears), MongoDB `slackvault_events` (1-hour TTL) |

---

### Step 6: Mention Strip

`bot_user_id` check + `text_clean = event.text.replace(f"<@{bot_user_id}>", "").strip()`

| Field | Value |
|---|---|
| **Input** | Raw Slack message text (e.g. `"@SlackVault add KEY=val to app in dev"`) |
| **Output** | Cleaned text (e.g. `"add KEY=val to app in dev"`) |
| **Latency** | ~5us |
| **Notes** | Only runs if `bot_user_id` is resolved (set during startup via `auth.test`) |

---

### Step 7: Concurrency Limiter

**Method:** `await ConcurrencyLimiter.run("event_{id}", lambda: _process_event(...))`

| Field | Value |
|---|---|
| **Input** | Name string + async callback |
| **Output** | Return value of callback (Response) |
| **Latency** | ~1us if slot available; blocks if all slots taken |
| **Semaphore** | `asyncio.Semaphore(MAX_CONCURRENT_OPS)` — default 10 |
| **Blocking** | If 10 events are already processing, the 11th awaits here until a slot frees. No timeout — will wait indefinitely. |
| **Stats** | `GET /stats` → `available_concurrency_slots` shows remaining capacity |

---

### Step 8: Confirmation Check

**Method:** `await ConversationStore.check_for_confirmation(channel_id, thread_ts, user_id, text) -> ConfirmationResult`

| Field | Value |
|---|---|
| **Input** | Channel ID, thread TS, user ID, cleaned message text |
| **Output** | `ConfirmationResult` with `.action` in `{CONFIRMED, CANCELLED, NOT_APPLICABLE}` and optional `.pending` |
| **Latency** | ~50us (in-memory) / ~100ms (MongoDB fallback) |
| **Path CONFIRMED** | User said "yes"/"confirm" on a pending operation → proceeds to execution |
| **Path CANCELLED** | User said "no"/"cancel" → replies "Operation cancelled", logs to audit, returns |
| **Path NOT_APPLICABLE** | Not a confirmation/cancel response → continues to intent parsing |
| **TTL** | Pending confirmations expire after 600s (10 min) in both in-memory dict and MongoDB `slackvault_pending` |

---

### Step 9: DeepSeek LLM Intent Extraction

**Method:** `await IntentParser.parse(text_clean) -> Intent`

| Component | Function | Input | Output | Latency | Notes |
|---|---|---|---|---|---|
| `IntentParser.parse` | `IntentParser.parse(text)` | Slack message string | `Intent` dataclass | — | Calls `llm_client.extract_intent` with system prompt |
| `DeepSeekClient.extract_intent` | `llm_client.extract_intent(system_prompt, user_message)` | System prompt (from `system_prompt.txt`) + user message | Raw JSON string | ~1–3s | HTTPS POST to `api.deepseek.com/v1/chat/completions` |
| JSON parsing | `json.loads(raw)` | Raw LLM response | Parsed dict | ~50us | |
| Retry logic | On `json.JSONDecodeError` | — | — | ~2s extra | Retries once with stricter "Return ONLY JSON" prompt |
| Fallback | On 2nd parse failure | — | `Intent(needs_clarification=True)` | — | Returns "I had trouble understanding" |
| **Intent dataclass** | `Intent.from_dict(data)` | Parsed dict | `Intent` with fields: `irrelevant`, `reject`, `reject_reason`, `needs_clarification`, `clarification_question`, `confirmation_response`, `app_name`, `environment`, `operation`, `key`, `value`, `new_key`, `raw_message` | ~10us | All fields default to False/None |

**System prompt file:** `src/agent/prompts/system_prompt.txt` — ~2000 tokens with 6 few-shot examples.

---

### Step 10: Intent Validation

**Method:** `IntentValidator.validate(intent) -> ValidationResult`

| Rule | Condition | Validation Error Message |
|---|---|---|
| Environment | Must be `dev` or `stage` | `"Environment X is not supported"` |
| App name | Must not be empty | `"I couldn't determine which app"` |
| Operation | Must be one of: `add`, `update`, `replace`, `append`, `rename_key`, `delete_key` | `"Operation X is not supported"` |
| Key | Must not be empty | `"Which environment variable key?"` |
| Value (for add/update/replace/append) | Must not be empty | `"What value should I set for X?"` |
| new_key (for rename_key) | Must not be empty | `"What should the new key name be?"` |
| `irrelevant` flag | True → passes through | — |
| `reject` flag | True → passes through | — |
| `needs_clarification` flag | True → passes through | — |
| **Latency** | ~20us | — |

**Failure flow:** Logs audit event `status=rejected` → Slack reply with clarification question → returns 200.

---

### Step 11: App Registry Resolution

**Method:** `AppRegistry.resolve(app_name) -> Optional[str]`

| Field | Value |
|---|---|
| **Input** | `app_name: str` (from Intent, extracted by LLM) |
| **Output** | Secret path template `"slackvault/{environment}/app-name"` or `None` |
| **Resolution order** | 1. Exact match on YAML+hybrid aliases (fast path) / 2. Fuzzy match (Levenshtein <= 2) / 3. Lazy refresh: if miss + >30s since last refresh + AWS loaded → re-discover from SM / 4. Retry resolve after refresh / 5. Return None if still no match |
| **Latency (hit)** | ~5us — pure Python dict/set lookup |
| **Latency (miss + refresh)** | ~500ms — calls `list_secrets()` from AWS via `asyncio.to_thread` |
| **Refresh cooldown** | 30s minimum between refreshes (avoids rapid re-discovery on consecutive misses) |
| **YAML mtime** | On refresh, checks `os.path.getmtime()`. If changed, re-reads `config/app-registry.yaml` |
| **Failure path** | Logs audit `rejected` → Slack reply with known app list → returns |

---

### Step 12: Environment Substitution

```python
resolved_path = resolved_path_template.replace("{environment}", intent.environment or "")
```

| Input | Output | Latency |
|---|---|---|
| `resolved_path_template: str` ("slackvault/{environment}/payments-service"), `environment: str` ("dev") | `resolved_path: str` ("slackvault/dev/payments-service") | ~1us |

---

### Step 13: Pending Confirmation Storage

**Method:** `await ConversationStore.store(message_ts, PendingConfirmation(...))`

| Field | Value |
|---|---|
| **Input** | `message_ts: str`, `PendingConfirmation` (intent, secret_path, channel_id, thread_ts, user_id, user_name) |
| **Output** | None |
| **Latency** | ~50us in-memory + ~50ms MongoDB write (fire-and-forget) |
| **Storage** | In-memory dict keyed by `message_ts` + MongoDB `slackvault_pending` with TTL index (600s) |
| **Next step** | Slack confirmation prompt sent to user — waits for reply |

---

### Step 14: Slack Confirmation Prompt

**Method:** `await SlackResponder.reply_confirmation_request(intent, resolved_path, channel, thread_ts, user) -> dict`

| Field | Value |
|---|---|
| **Input** | Intent dataclass, resolved path, Slack channel/thread metadata |
| **Output** | Slack API response dict (`chat.postMessage` result) |
| **Latency** | ~500ms (HTTP POST to `slack.com/api/chat.postMessage`) |
| **Slack payload** | Formatted text with operation description, app, environment, secret path, and confirm/cancel instructions |

**User now sees the prompt and must reply "yes" or "cancel" in the same thread.**

User typically takes **2–60 seconds** to respond. Confirmation expires after **600 seconds (10 minutes)**.

---

### Step 15: Confirmation Response (Async continuation)

**Method:** `await ConversationStore.check_for_confirmation(channel_id, thread_ts, user_id, "yes") -> ConfirmationResult(CONFIRMED, pending)`

Same as Step 8 but returns `CONFIRMED` action.

---

### Step 16: Per-Secret Lock Acquisition

**Method:** `await SecretLockManager.execute_locked(secret_path, operation, callback)`

| Field | Value |
|---|---|
| **Input** | `secret_path: str`, `operation_name: str`, `callback: async callable` |
| **Output** | Return value of callback |
| **Latency** | ~1us if lock free; waits if another concurrent request holds lock for same secret |
| **Lock granularity** | One `asyncio.Lock` per unique secret path (case-insensitive) |
| **Parallelism** | Different secrets run in parallel; same secret serializes |
| **Metrics** | `GET /stats` → `active_locks` (# of unique paths currently tracked), `waiting_locks` (# of operations awaiting locks) |

---

### Step 17: AWS Secrets Manager Operation

**Method:** `await SecretsManagerClient.execute_operation(intent) -> dict`

| Sub-step | Function | Input | Output | Latency | Notes |
|---|---|---|---|---|---|
| 17a | `asyncio.to_thread(get_secret_with_version)` | `secret_path: str` | `(data: dict, version_id: str)` | ~300ms | Reads current secret JSON blob + its `VersionId` |
| 17b | Modify in-memory | Intent fields, current data | Modified dict | ~10us | Add/update/replace/rename/delete key |
| 17c | `asyncio.to_thread(put_secret_safe)` | `(secret_path, modified_data, version_id)` | `new_version_id: str` | ~300ms | Writes with `ClientRequestToken=version_id` for optimistic locking |
| 17d | Conflict detection | — | `{"status": "conflict", "message": ...}` | ~10us | Only for `add` on existing key |
| 17e | Key-not-found | — | `{"status": "error"}` or `{"status": "skipped"}` | ~10us | For delete/rename on nonexistent key |
| 17f | Optimistic lock retry | On `PreconditionFailed` error | Retry from 17a | +~600ms per retry | Up to 3 retries with exponential backoff (0.1s, 0.2s, 0.4s) |
| 17g | Throttle retry | On `ThrottlingException` | Retry via `_retry_sync` | +1–8s | Up to 3 retries (1s, 2s, 4s backoff) |
| **Total (no retries)** | — | — | `{"status": "success", "secret_path": str, "version_id": str}` | **~600ms** | One read + one write |
| **Total (with lock retry)** | — | — | `{"status": "success"}` or `{"status": "error", "message": "Too many concurrent updates"}` | ~2s | Worst case: 3 read+write cycles with backoff |

**AWS SM API costs (per operation):**
- `GetSecretValue`: $0.000005 per call (10,000 for $0.05)
- `PutSecretValue`: $0.000005 per call
- `ListSecrets`: $0.000005 per call

---

### Step 18: Audit Log Write

**Method:** `await MongoAuditLogger.log(intent, status, secret_path, ...)`

| Field | Value |
|---|---|
| **Input** | Intent, status string, secret path, version ID, error message, Slack user/channel metadata |
| **Output** | None (fire-and-forget to MongoDB) |
| **Latency** | ~50ms (MongoDB insert) |
| **Storage** | MongoDB collection `slackvault_audit` — one document per operation |
| **Failure handling** | Logs warning, continues without blocking the Slack reply |

**Document schema:**
```json
{
  "_id": "uuid",
  "created_at": "ISO8601",
  "slack_user_id": "U001",
  "slack_user_name": "john.doe",
  "channel_id": "C001",
  "message_ts": "1234567890.123456",
  "app_name": "payments-service",
  "environment": "stage",
  "operation": "replace",
  "secret_path": "slackvault/stage/payments-service",
  "key_name": "DB_PASSWORD",
  "status": "success",
  "error_message": null,
  "sm_version_id": "v1"
}
```

---

### Step 19: Slack Success Reply

**Method:** `await SlackResponder.reply_success(intent, resolved_path, channel, thread_ts, user)`

| Field | Value |
|---|---|
| **Input** | Intent, secret path, Slack channel/thread metadata |
| **Output** | Slack API response (ignored) |
| **Latency** | ~500ms (HTTP POST to `slack.com/api/chat.postMessage`) |
| **Slack payload** | Formatted success message with app, environment, operation, key, SM path, requester, timestamp |

---

## Full Timing Summary

### Successful Request (First attempt, no retries)

| Step | Component | Latency | Cumulative |
|---|---|---|---|
| 1 | ALB network | ~2ms | ~2ms |
| 3 | Signature verify | ~0.05ms | ~2ms |
| 4 | Event parse | ~0.01ms | ~2ms |
| 5 | Dedup check | ~0.005ms | ~2ms |
| 6 | Mention strip | ~0.005ms | ~2ms |
| 7 | Concurrency limiter | ~0.001ms | ~2ms |
| 8 | Confirmation check | ~0.05ms | ~2ms |
| 9 | DeepSeek LLM | ~2s | ~2s |
| 10 | Intent validation | ~0.02ms | ~2s |
| 11 | App registry resolve | ~0.005ms | ~2s |
| 12 | Path substitution | ~0.001ms | ~2s |
| 13 | Store confirmation | ~1ms | ~2s |
| 14 | Slack reply (prompt) | ~500ms | ~2.5s |
| | *User thinks for ~5s* | ~5s | ~7.5s |
| 15 | Confirmation check (reply) | ~0.05ms | ~7.5s |
| 16 | Lock acquisition | ~0.001ms | ~7.5s |
| 17a | SM read | ~300ms | ~7.8s |
| 17c | SM write | ~300ms | ~8.1s |
| 18 | Audit log | ~50ms | ~8.15s |
| 19 | Slack reply (success) | ~500ms | ~8.65s |
| **Total** | | | **~5–12s** |

### Fastest possible (user confirms instantly)
~3.5s (Step 9 is the bottleneck — DeepSeek API at ~1s minimum)

### Worst case (throttling + lock contention + retries)
~30s (multiple SM retries + DeepSeek timeout retries)

---

## Concurrency Model

### Capacity

| Level | Limit | Unit |
|---|---|---|
| Replicas | 3 | Pods |
| Concurrency per pod | 10 (configurable via `MAX_CONCURRENT_OPS`) | Events |
| **Total concurrent events** | **30** | Simultaneous Slack event pipelines |
| Per-secret locks | Unlimited | One `asyncio.Lock` per unique secret path |
| DeepSeek API rate limit | Depends on plan (typically ~10k req/min) | Requests per minute |
| AWS SM API rate limit | ~5000 req/s per region | Requests per second |

### Throughput Estimates

| Scenario | QPS | Notes |
|---|---|---|
| **All 30 slots busy, no queuing** | ~3 QPS (request only, no confirmation) | 30 concurrent / ~10s per request = 3 req/s |
| **With confirmation loop** | ~0.3 QPS sustained | Each request takes ~10s round-trip including user think time |
| **Burst (10 concurrent new requests)** | ~10 QPS burst | All hit the concurrency limiter, queue behind the 10-slot semaphore per pod |
| **Continuous operation execution** | ~50 ops/second | Steps 17a–17c take ~600ms total, and each pod has 10 slots: 10 / 0.6s = ~16 ops/sec per pod × 3 = ~50/s |
| **DeepSeek API calls** | ~10 req/s max | 30 slots / (~3s per call) = ~10 req/s peak |

### Scaling

| To handle | Action |
|---|---|
| More concurrent requests | Increase `replicas` or `MAX_CONCURRENT_OPS` per pod |
| Higher DeepSeek throughput | Increase `MAX_CONCURRENT_OPS` (monitor API rate limits) |
| More apps/secrets | Not a bottleneck — `_hybrid` dict search is O(n) with ~us latency even for 10K entries |
| Secret write contention | Increase `OPTIMISTIC_LOCK_RETRIES` or reduce `MAX_CONCURRENT_OPS` per secret |

