# SlackVault — Sandbox Testing Guide

## Overview

Sandbox testing validates core SlackVault functionality against a real AWS account (sandbox/isolated) with local Docker hosting. No EKS or IRSA needed — the bot connects to AWS SM via standard AWS credentials and is exposed via ngrok.

**Goal:** Verify every operation works end-to-end before any EKS deployment.

---

## Architecture (Sandbox)

```
Slack → ngrok → localhost:8000 (FastAPI in Docker)
                    ├── AWS SM (sandbox AWS account, direct boto3)
                    ├── MongoDB (Docker container)
                    └── DeepSeek API
```

---

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Docker Desktop | >= 24 | Runs SlackVault + MongoDB containers |
| ngrok | Latest | Exposes local FastAPI to Slack's webhook |
| AWS CLI | >= 2 | Create test secrets, verify results |
| Python | >= 3.12 | For `pytest` if running tests locally |

**AWS account:** A sandbox or isolated AWS account. No production secrets can exist in this account.

---

## Infrastructure Setup

### 1. Create a Sandbox Slack App

Go to https://api.slack.com/apps → **Create New App** → From scratch.

| Setting | Value |
|---|---|
| App Name | `SlackVault-Sandbox` |
| Workspace | Your engineering workspace |

**OAuth Scopes** (Bot Token Scopes):
```
channels:history
groups:history
chat:write
reactions:write
users:read
```

**Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-...`).

**Event Subscriptions** → Enable → Set Request URL to `https://placeholder.ngrok.io/slack/events` (update after ngrok starts).

Subscribe to bot events:
```
message.channels
message.groups
```

**Basic Information** → copy the **Signing Secret**.

### 2. Create MongoDB (Local Docker)

Already included in `docker-compose.yml`. Just runs on port 27017.

> No manual setup needed. The app creates the `slackvault_audit`, `slackvault_events`, and `slackvault_pending` collections and indexes automatically on startup.

### 3. Create AWS Secrets Manager Test Secrets

In your **sandbox AWS account**, create secrets that match your `app-registry.yaml`. For example:

```bash
# Create a dev secret
aws secretsmanager create-secret \
    --name "dev/payments-service" \
    --secret-string '{"DB_HOST":"localhost","DB_PASSWORD":"test123","API_KEY":"sandbox-key"}' \
    --region us-east-1

# Create a stage secret
aws secretsmanager create-secret \
    --name "stage/payments-service" \
    --secret-string '{"DB_HOST":"stage.db.internal","DB_PASSWORD":"stage-pass","API_KEY":"stage-key"}' \
    --region us-east-1

# Create an auth service secret
aws secretsmanager create-secret \
    --name "dev/auth-service" \
    --secret-string '{"AUTH_URL":"http://auth.local","TOKEN":"dev-token"}' \
    --region us-east-1
```

**Verify** the secrets exist:
```bash
aws secretsmanager list-secrets --region us-east-1 | jq '.SecretList[].Name'
```

### 4. Configure Local Environment

```bash
cp .env.example .env
```

Edit `.env` — **comment out `APP_SECRET_ARN`** (we're using local env vars, not SM config loading):

```env
# APP_SECRET_ARN=arn:aws:...

SLACK_SIGNING_SECRET=your-sandbox-slack-signing-secret
SLACK_BOT_TOKEN=xoxb-your-sandbox-bot-token
DEEPSEEK_API_KEY=sk-your-deepseek-key
DEEPSEEK_API_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
ALLOWED_CHANNEL_IDS=C012XY345     # Your sandbox Slack channel ID
DB_URL=mongodb://localhost:27017
MONGO_DB_NAME=slackvault
TRIGGER_MODE=mention
AUTO_CREATE_SECRET=true
MAX_CONCURRENT_OPS=10
LOG_LEVEL=DEBUG

AWS_ACCESS_KEY_ID=AKIA...         # Sandbox AWS credentials
AWS_SECRET_ACCESS_KEY=...          # Sandbox AWS credentials
AWS_DEFAULT_REGION=us-east-1
```

> **Security note:** Never commit `.env`. It's already in `.gitignore`.

---

## Running the App

### 1. Start Docker Compose

```bash
docker-compose up
```

This starts:
- **MongoDB** on `localhost:27017`
- **SlackVault** on `localhost:8000`

Watch the logs for:
```
Connected to MongoDB
Discovered X secrets from AWS SM
Hybrid registry: X apps (X from YAML, X from AWS)
Resolved bot user_id: B...
```

### 2. Expose with ngrok

In a separate terminal:

```bash
ngrok http 8000
```

Copy the ngrok URL (e.g., `https://abc123.ngrok.io`).

### 3. Update Slack App Request URL

Go to your Slack App → **Event Subscriptions** → set Request URL to:

```
https://abc123.ngrok.io/slack/events
```

Slack will send a `url_verification` challenge. You should see in the Docker logs:

```
Handling url_verification challenge
```

The URL will show as **Verified** in Slack.

### 4. Invite the Bot to Your Channel

```
/invite @SlackVault-Sandbox
```

---

## Test Scenarios

Run these tests in order. Each test should succeed before moving to the next.

### Test 1: Add a new key

```
@SlackVault-Sandbox add TEST_KEY=hello-world to payments in dev
```

**Expected:**
```
🔎 Please confirm:
  Add `TEST_KEY` = `hello-world`
  App:          payments-service
  Environment:  dev
  Secret:       dev/payments-service
  ...
```

Reply `yes`.

**Expected:**
```
✅ Done!
  App:          payments-service
  Environment:  dev
  Operation:    add
  Key:          TEST_KEY
  SM Path:      dev/payments-service
```

**Verify:**
```bash
aws secretsmanager get-secret-value --secret-id dev/payments-service \
    --query SecretString --output text | python3 -m json.tool
# Should show TEST_KEY
```

### Test 2: Update an existing key

```
@SlackVault-Sandbox update DB_HOST to db.sandbox.internal for payments in dev
```

Confirm → verify the value changed in AWS SM.

### Test 3: Rename a key

```
@SlackVault-Sandbox rename TEST_KEY to TEST_KEY_RENAMED in payments dev
```

Confirm → verify the old key is gone and the new key exists with the same value.

### Test 4: Delete a key

```
@SlackVault-Sandbox remove TEST_KEY_RENAMED from payments in dev
```

Confirm → verify the key is gone.

### Test 5: Reject production

```
@SlackVault-Sandbox add BAD=thing to payments in prod
```

**Expected:**
```
🚫 Rejected: Production secrets are not managed by SlackVault.
```

### Test 6: Unknown app

```
@SlackVault-Sandbox add KEY=val to nonexistent-app in dev
```

**Expected:**
```
🤔 Unknown app 'nonexistent-app'. Known apps: payments-service, auth-service
```

### Test 7: Confirm cancellation

```
@SlackVault-Sandbox add CANCEL_TEST=val to payments in dev
```

See the confirmation prompt → reply `cancel`.

**Expected:**
```
🚫 Operation cancelled.
```

Verify no new key was added to the secret.

### Test 8: Mention mode (default)

Send `add KEY=val to payments in dev` **without** `@SlackVault-Sandbox`.

**Expected:** No response (bot ignores non-mention messages in mention mode).

### Test 9: Confirm with wrong user (in a thread)

User A requests an operation → User B replies `yes` in the same thread.

**Expected:** Nothing (the conversation store only matches the original requester). The operation is not executed. User A can still confirm.

### Test 10: Expired confirmation

Request an operation. Wait 10 minutes. Reply `yes`.

**Expected:** `⏰ Confirmation expired. Please send your request again.` (if MongoDB fallback picks it up) or `❌ I don't have a pending operation to confirm...` (if already cleaned up).

---

## Running Unit Tests

In a separate terminal (outside Docker):

```bash
pip install -r requirements.txt
pytest tests/ -v
```

Expected: **all 8 test files pass**.

---

## Cleanup

After sandbox testing:

```bash
# Stop Docker Compose
docker-compose down

# Delete test secrets
aws secretsmanager delete-secret --secret-id dev/payments-service --force-delete-without-recovery
aws secretsmanager delete-secret --secret-id stage/payments-service --force-delete-without-recovery
aws secretsmanager delete-secret --secret-id dev/auth-service --force-delete-without-recovery

# Delete the Slack app
# Go to https://api.slack.com/apps → SlackVault-Sandbox → Settings → Delete App
```

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Slack URL not verified | ngrok running? URL correct in Slack? Check Docker logs for `url_verification` |
| "No SM client" in logs | AWS credentials missing in `.env` |
| Unknown app | Secret doesn't exist in AWS SM, or name doesn't match `app-registry.yaml` aliases |
| LLM returning weird JSON | Check DeepSeek API key is valid |
| `pytest` fails | Run from project root, `pip install -r requirements.txt` first |
| `motor` import error | `pip install motor` — it's in requirements.txt |