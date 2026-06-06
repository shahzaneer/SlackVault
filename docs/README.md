# SlackVault User Guide

SlackVault is an AI bot that manages your AWS Secrets Manager secrets through natural-language Slack messages. It watches an engineering Slack channel, understands requests like "add DB_HOST=mydb.internal to the payments app in stage", asks you to confirm, then executes the change and reports back.

---

## Quick Start

### 1. Find SlackVault in your channel

SlackVault sits in your engineering Slack channel. Look for the bot named **@SlackVault** in the member list.

### 2. Mention it with a request

```
@SlackVault add DB_HOST=mydb.internal to the payments app in dev
```

### 3. SlackVault asks you to confirm

```
🔎 Please confirm:
  Add `DB_HOST` = `mydb.internal`
  App:          payments-service
  Environment:  dev
  Secret:       slackvault/dev/payments-service
  Requested by: @you

Reply yes to confirm or cancel to abort. This request expires in 10 minutes.
```

### 4. Reply `yes` to execute

```
✅ Done!
  App:          payments-service
  Environment:  dev
  Operation:    add
  Key:          DB_HOST
  SM Path:      slackvault/dev/payments-service
  Requested by: @you
  Time:         2026-06-06T12:00:00Z
```

Or reply `cancel` to abort:

```
🚫 Operation cancelled.
```

---

## Supported Commands

Every SlackVault command follows this pattern:
```
@SlackVault [operation] [details] to [app] in [environment]
```

### Add a new key

```
@SlackVault add DB_HOST=mydb.internal to the payments app in dev
@SlackVault include REDIS_URL=redis://localhost to auth service in stage
```

### Update / replace an existing key

```
@SlackVault update DB_PASSWORD for payments in staging to newpass123
@SlackVault change API_KEY to abcdef for api-gateway in dev
@SlackVault set MAX_CONNECTIONS=200 for user-service in stage
@SlackVault replace DB_HOST with db.internal in payments dev
```

### Append a key (add even if it might exist)

```
@SlackVault append DEBUG=true to auth in development
```

### Rename a key

```
@SlackVault rename DATABASE_URL to DB_URL in payments, stage
@SlackVault change the key name from CACHE_TTL to CACHE_TIMEOUT in auth dev
```

### Delete a key

```
@SlackVault remove TIMEOUT from payments in dev
@SlackVault delete the API_KEY key from user-service in stage
```

---

## Supported Environments

SlackVault operates on **two environments only**:

| Identifier | Accepted Aliases |
|---|---|
| `dev` | `dev`, `development`, `develop` |
| `stage` | `stage`, `staging`, `stg` |

Any request mentioning `prod` or `production` is rejected:

```
🚫 Rejected: Production secrets are not managed by SlackVault.
```

---

## How Confirmation Works

Every destructive operation follows a two-step confirmation flow:

1. **Request**: You send a request → SlackVault parses it with AI → sends a confirmation prompt
2. **Confirm**: You reply `yes` or `cancel` in the same thread → SlackVault executes or aborts

- Pending confirmations **expire after 10 minutes**
- Only the **original requester** can confirm or cancel (security)
- The **same thread** must be used for the confirmation reply
- Both confirmations and cancellations are **audit-logged**

If you say "yes" without a pending confirmation:

```
❌ I don't have a pending operation to confirm. Please send your request again.
```

---

## What Happens When...

### The app name is ambiguous

If SlackVault isn't sure which app you mean:

```
🤔 I found multiple apps matching 'pay'. Which one did you mean?
  • slackvault/dev/payments-service
  • slackvault/dev/payroll-service

Please rephrase your request with the exact app name.
```

### The environment is wrong (not dev/stage)

```
🚫 Rejected: Production secrets are not managed by SlackVault.
```

### The key already exists (on add)

```
⚠️ Key 'DB_HOST' already exists. Reply 'replace' to overwrite or 'cancel' to abort.
```

### The key doesn't exist (on delete/rename)

```
❌ Key 'DB_HOST' not found in slackvault/dev/payments-service
```

### Required information is missing

```
🤔 Which app should LOG_LEVEL=debug be added to in stage?
Known apps: slackvault/{environment}/payments-service, slackvault/{environment}/auth-service
```

### The app is unknown

```
🤔 Unknown app 'my-app'. Known apps: payments-service, auth-service, api-gateway, user-service
```

### The AI is temporarily unavailable

```
❌ AI service temporarily unavailable. Please try again shortly.
```

---

## Bot Interaction Modes

SlackVault can run in two modes:

| Mode | How to activate | Best for |
|---|---|---|
| **Mention** (default) | `@SlackVault add KEY=val to app in env` | Busy channels — only triggers when explicitly mentioned |
| **Passive** | Just type the request naturally | Dedicated SlackVault channels — reads every message and decides what's relevant |

Your team's SlackVault operator chooses the mode. You don't need to configure anything.

---

## App Name Resolution

SlackVault knows your apps in two ways:

1. **Pre-configured aliases** — your team maintains a mapping in `app-registry.yaml`:
   - `payments` → `payments-service`
   - `auth` → `auth-service`
   - `api-gateway` → `gateway`

2. **Auto-discovery** — SlackVault scans AWS Secrets Manager on startup and adds any secret under `slackvault/{env}/{app-name}`

Both exact names and typical shorthand work: "payments", "payment", "payments service", "payments-service" all resolve to the same secret.

---

## Security & Privacy

| Aspect | How SlackVault Handles It |
|---|---|
| **Slack message contents** | Sent to DeepSeek API for intent extraction. SlackVault never stores your raw messages permanently. |
| **Secret values** | SlackVault never echoes secret values in Slack replies — only key names are shown. |
| **Who can confirm** | Only the original requester in the original thread. No one else can confirm or cancel your operation. |
| **Who can use SlackVault** | Only members of the configured channel(s). Controlled by the `ALLOWED_CHANNEL_IDS` setting. |
| **Audit trail** | Every operation (success, rejection, cancellation) is logged to MongoDB with the requester's Slack user ID, timestamp, and operation details. |
| **Production secrets** | Hard-blocked at the code level. SlackVault does not interact with any secret outside the `slackvault/` prefix. |
| **Message integrity** | Every Slack webhook is verified using HMAC-SHA256. Impersonation is not possible. |

---

## Known Limitations

| Limitation | Explanation |
|---|---|
| **No `prod` environment** | Intentional safety measure. Production secrets must be managed through existing runbooks. |
| **10-minute confirmation expiry** | Pending confirmations expire. If you take too long, resend the request. |
| **One key per operation** | You cannot add/update/delete multiple keys in a single message. Each operation handles one key. |
| **JSON blob secrets only** | Secrets must be stored as JSON key-value pairs (`{"KEY": "val"}`). SlackVault cannot manage binary or plaintext secrets. |
| **SlackVault path prefix** | Only secrets under `slackvault/` prefix are manageable. Other secrets in your account are invisible to SlackVault. |
| **No rollback** | Operations are not reversible. Download a backup of your secret before making changes if needed. |
| **Dedicated channel recommended** | In mention mode, any channel where the bot is a member works. But a dedicated SlackVault channel reduces noise. |
| **English messages only** | The LLM system prompt is in English. Non-English requests may not parse correctly. |

---

## Frequently Asked Questions

**Q: Can SlackVault read my existing secrets?**
A: SlackVault can list secret names under the `slackvault/` prefix, but it only reads the actual secret values when performing an operation you explicitly request and confirm. Users cannot ask "what are my secrets?" and get values back.

**Q: Does SlackVault work in private channels?**
A: Yes. SlackVault needs the `groups:history` OAuth scope and must be invited to the private channel.

**Q: What if I reply `yes` to the wrong confirmation?**
A: No rollback is available. The operation executes immediately on confirmation. Use the confirmation prompt to verify the operation — it shows the exact app, environment, key, operation, and secret path before you confirm.

**Q: How fast is SlackVault?**
A: A typical request → confirmation → execution cycle takes 3–10 seconds:
- ~1–3s: DeepSeek API response time
- ~0.5s: AWS Secrets Manager API call
- ~1s: Slack API reply

**Q: What happens if SlackVault is down?**
A: Slack events are retried. If the pod returns non-200, Slack retries delivery for up to 3 days. Once SlackVault recovers, pending events are processed.

**Q: Can I see a history of changes?**
A: Yes. Every operation is logged to a MongoDB collection `slackvault_audit`. Your team can query it directly to see who did what, when, on which app and environment.

**Q: Does SlackVault store my Slack messages?**
A: SlackVault stores your Slack message text temporarily in memory during processing, but it's never persisted. Only the extracted operation details (app name, environment, key, operation type, status) are stored in the audit log.

**Q: Can I use SlackVault through a message thread?**
A: Yes. Just start a new thread with a mention or work directly in the channel. Confirmation responses must be in the same thread as the original request.

---

## Getting Help

Contact your team's SlackVault operator. The operator can:
- Check whether the bot is running (`/readyz` endpoint)
- See live stats (`/stats` endpoint)
- View logs for troubleshooting
- Add or remove allowed channel IDs
- Update the app alias registry

---

## Performance & Capacity

| Metric | Value |
|---|---|
| Typical request-to-confirmation time | ~2–3 seconds |
| Typical confirmation-to-execution time | ~1 second |
| Full round-trip (including user think time) | ~5–12 seconds |
| Max concurrent operations | 30 (3 replicas × 10 slots) |
| App discovery | Auto-scanned from AWS SM at startup + lazy refresh on unknown apps |

For detailed step-by-step timing of every function call, see `FLOW_OF_EXECUTION.md`.