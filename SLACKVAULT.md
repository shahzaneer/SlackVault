# SlackVault
### AI-Powered Slack → AWS Secrets Manager Automation Agent

> **SlackVault** watches your engineering Slack channel, understands natural-language secret update requests, and autonomously applies the correct operation on AWS Secrets Manager — with full audit trail and zero manual login.

---

## Table of Contents

1. [Overview](#1-overview)
2. [System Architecture](#2-system-architecture)
3. [Component Breakdown](#3-component-breakdown)
4. [AI Agent Design](#4-ai-agent-design)
5. [Supported Intent Patterns](#5-supported-intent-patterns)
6. [Secret Operation Logic](#6-secret-operation-logic)
7. [AWS Infrastructure](#7-aws-infrastructure)
8. [EKS Deployment](#8-eks-deployment)
9. [Configuration Loading at Startup](#9-configuration-loading-at-startup)
10. [Security Model](#10-security-model)
11. [Slack Bot Configuration](#11-slack-bot-configuration)
12. [Audit & Observability](#12-audit--observability)
13. [Error Handling & Edge Cases](#13-error-handling--edge-cases)
14. [Terraform — Risks & Guidance](#14-terraform--risks--guidance)
15. [Pricing Breakdown](#15-pricing-breakdown)
16. [Project Folder Structure](#16-project-folder-structure)
17. [Implementation Roadmap](#17-implementation-roadmap)

---

## 1. Overview

### Problem Statement

Engineering teams routinely need to update environment variables stored in AWS Secrets Manager for various applications across `dev` and `stage` environments. Currently this requires logging into the AWS Console or CLI, navigating to the right secret, and applying the change manually for every request.

Requests arrive in a Slack channel in free-form natural language, in any order, with varying phrasing. This is slow and a distraction for DevOps engineers.

### Solution: SlackVault

SlackVault is an AI agent service that:

1. **Listens** to a designated Slack channel via the Slack Events API
2. **Understands** natural-language secret update requests using DeepSeek LLM
3. **Resolves** the correct AWS Secrets Manager path for the target app + environment
4. **Executes** the correct secret operation (add, update, replace, rename key, delete key)
5. **Confirms** the action back in Slack with a threaded reply and audit log entry in MongoDB

### Supported Environments

SlackVault strictly operates on **two environments only**:

| Identifier | Accepted Aliases |
|---|---|
| `dev` | `dev`, `development`, `develop` |
| `stage` | `stage`, `staging`, `stg` |

Any request mentioning `prod`, `production`, or any other environment is **rejected with an explanation**.

---

## 2. System Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                          SLACK WORKSPACE                               │
│  "add DB_HOST=mydb.internal to the payments app in stage"              │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │  Slack Events API (HTTP POST)
                               ▼
                 ┌─────────────────────────┐
                 │  AWS ALB (L7 Load        │
                 │  Balancer — public)      │
                 │  /slack/events route     │
                 └────────────┬────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│              SlackVault Service  (EKS Pod / Deployment)             │
│                                                                     │
│  ┌──────────────────┐   ┌─────────────────┐   ┌─────────────────┐  │
│  │  Slack Handler   │──▶│  AI Agent Core  │──▶│  AWS SM Client  │  │
│  │  (event router)  │   │  (DeepSeek LLM) │   │  (boto3)        │  │
│  └──────────────────┘   └────────┬────────┘   └────────┬────────┘  │
│                                  │                      │           │
│                         ┌────────▼────────┐             │           │
│                         │ Intent Validator│             │           │
│                         └────────┬────────┘             │           │
│                                  │                      ▼           │
│  ┌──────────────────┐            │            ┌─────────────────┐   │
│  │  Slack Responder │◀───────────┘            │  Audit Logger   │   │
│  │  (thread reply)  │                         │  (MongoDB)       │   │
│  └──────────────────┘                         └─────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
          │                            │
          ▼                            ▼
 ┌─────────────────┐          ┌────────────────┐
 │  AWS Secrets    │          │  CloudWatch    │
 │  Manager        │          │  Logs          │
 └─────────────────┘          └────────────────┘
```

### Data Flow Summary

```
Slack Message
    → ALB (public HTTPS endpoint, no extra cost beyond the ALB you likely already have)
    → SlackVault Pod (signature verify + event parse)
    → Deduplication check (in-memory cache or DB query on event_id)
    → Strip @mention prefix in mention mode
    → LLM Intent Extraction (DeepSeek API)
    → Intent Validation (env guard, app name resolution)
    → App Registry: resolve app alias → SM path template, substitute {environment}
    → AWS Secrets Manager operation (with retry/backoff on throttling)
    → Audit log write (MongoDB)
    → Slack thread reply (success / failure / rejection)
```

---

## 3. Component Breakdown

### 3.1 Slack Handler

Responsible for:
- Receiving raw HTTP POST events from Slack
- Verifying the `X-Slack-Signature` HMAC header using the Slack signing secret
- Responding to `url_verification` challenges (one-time Slack setup step)
- Filtering event types — only `message` events from the configured channel pass through
- Deduplicating events using Slack's `event_id` (Slack retries delivery on non-200 responses)
- Ignoring bot messages to prevent self-loops
- In `mention` mode, resolving the bot's own user ID at startup via `auth.test` API and stripping the `@SlackVault` mention prefix before sending text to the LLM

### 3.2 AI Agent Core (DeepSeek LLM)

The brain of the system. Takes raw Slack message text and returns a structured JSON intent.

**Responsibilities:**
- Extract `app_name`, `environment`, `operation`, `key`, `value`, `new_key`
- Handle ambiguous phrasing ("change", "update", "set", "replace", "add", "remove")
- Normalize environment names to `dev` or `stage`
- Silently ignore messages that are clearly not secret-related
- Return a clarification question if required fields are missing

**Model:** `deepseek-chat` (or `deepseek-coder`) via the DeepSeek API

### 3.3 Intent Validator

Deterministic rule-based layer that runs after LLM extraction:

- `environment` must be strictly `dev` or `stage`
- `app_name` must resolve to a known secret path via the app registry
- `operation` must be one of the defined enum values
- For `update`/`replace`: both `key` and `value` must be present
- For `rename_key`: both `key` (old) and `new_key` must be present
- For `delete_key`: only `key` is required

Validation failures generate a Slack reply asking for clarification — never a silent failure.

### 3.4 AWS Secrets Manager Client

Thin `boto3` wrapper performing all secret operations.

| Operation | SM Action |
|---|---|
| `add` | `GetSecretValue` → merge new key → `PutSecretValue` |
| `update` / `replace` | `GetSecretValue` → overwrite key → `PutSecretValue` |
| `append` | Same as `add` — no guard on existing key |
| `rename_key` | `GetSecretValue` → copy value to new key, delete old key → `PutSecretValue` |
| `delete_key` | `GetSecretValue` → remove key → `PutSecretValue` |

> All operations modify keys **within** the JSON blob of a secret. The secret ARN itself is never deleted.

### 3.5 Audit Logger

Every operation writes a record to **MongoDB**. The `MongoAuditLogger` connects via `motor` (async driver) and auto-creates indexes on startup.

**MongoDB collection: `slackvault_audit`**

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

**Auto-created indexes:**
- `{ app_name: 1, environment: 1 }` — `idx_audit_app_env`
- `{ slack_user_id: 1 }` — `idx_audit_user`
- `{ created_at: -1 }` — `idx_audit_time`

---

## 4. AI Agent Design

### 4.1 System Prompt

```
You are SlackVault, an AI agent that manages AWS Secrets Manager updates on behalf of engineers.

Your job is to extract structured information from a Slack message and return a JSON object.

Rules:
1. Only extract requests for environment "dev" or "stage".
   Map aliases: development→dev, staging/stg→stage.
2. If the environment is "prod", "production", or anything other than dev/stage,
   set "reject": true and "reject_reason": "Production secrets are not managed by SlackVault."
3. If the message is clearly not about secrets or environment variables, set "irrelevant": true.
4. Operations:
   - "add"        → add a new key (did not exist before)
   - "update"     → update an existing key's value
   - "replace"    → same as update; used when user says replace/change/set
   - "append"     → add key even if it might already exist
   - "rename_key" → rename a key, preserve its value
   - "delete_key" → remove a key entirely
5. If required information is missing (no app name, no key name), set "needs_clarification": true
   and populate "clarification_question" with a specific, friendly question.
6. Return ONLY valid JSON. No explanation, no markdown fences.

Output schema:
{
  "irrelevant":             false,
  "reject":                 false,
  "reject_reason":          null,
  "needs_clarification":    false,
  "clarification_question": null,
  "app_name":               "<string>",
  "environment":            "dev" | "stage",
  "operation":              "add" | "update" | "replace" | "append" | "rename_key" | "delete_key",
  "key":                    "<env var key>",
  "value":                  "<value or null>",
  "new_key":                "<new key name if rename_key, else null>",
  "raw_message":            "<original message verbatim>"
}
```

### 4.2 Few-Shot Examples (included in system prompt)

**Add**
```
Input:  "can someone add DB_HOST=mydb.internal to the payments app in stage?"
Output: { "app_name": "payments-service", "environment": "stage", "operation": "add",
          "key": "DB_HOST", "value": "mydb.internal", ... }
```

**Replace with alias**
```
Input:  "update DB_PASSWORD for payments in staging to newpass123"
Output: { "app_name": "payments-service", "environment": "stage", "operation": "replace",
          "key": "DB_PASSWORD", "value": "newpass123", ... }
```

**Rename Key**
```
Input:  "rename DATABASE_URL to DB_URL in api-gateway, stage"
Output: { "app_name": "api-gateway", "environment": "stage", "operation": "rename_key",
          "key": "DATABASE_URL", "new_key": "DB_URL", ... }
```

**Missing app name**
```
Input:  "add LOG_LEVEL=debug in stage"
Output: { "needs_clarification": true,
          "clarification_question": "Which app should LOG_LEVEL=debug be added to in stage?", ... }
```

**Production rejection**
```
Input:  "set API_KEY=xyz in prod for user-service"
Output: { "reject": true, "reject_reason": "Production secrets are not managed by SlackVault.", ... }
```

**Irrelevant**
```
Input:  "standup at 10am today everyone"
Output: { "irrelevant": true, ... }
```

### 4.3 App Name Resolution

App names in Slack are fuzzy. SlackVault maintains an `app-registry.yaml` that maps aliases to SM paths:

```yaml
apps:
  - aliases: [payments, payment-service, payments-api, payment svc]
    secret_path: "slackvault/{environment}/payments-service"

  - aliases: [auth, auth-service, authentication]
    secret_path: "slackvault/{environment}/auth-service"

  - aliases: [api-gateway, gateway, api gateway]
    secret_path: "slackvault/{environment}/api-gateway"
```

`{environment}` is substituted at runtime with `dev` or `stage`.

Resolution order:
1. Exact match on alias list
2. Fuzzy match (Levenshtein distance ≤ 2)
3. No match → clarification reply listing known apps

---

## 5. Supported Intent Patterns

| User Says | Operation |
|---|---|
| "add X=Y to app in env" | `add` |
| "include X=Y in app env" | `add` |
| "set X to Y in app/env" | `replace` |
| "update X to Y for app" | `replace` |
| "change X to Y in app/env" | `replace` |
| "replace X with Y" | `replace` |
| "append X=Y to app in env" | `append` |
| "rename X to Z in app/env" | `rename_key` |
| "change key name X to Z" | `rename_key` |
| "remove X from app in env" | `delete_key` |
| "delete X from app secrets" | `delete_key` |

---

## 6. Secret Operation Logic

### Secret Path Convention

```
slackvault/{environment}/{app-name}
```

Examples:
- `slackvault/dev/payments-service`
- `slackvault/stage/auth-service`

Secrets are stored as **JSON key-value blobs** — one secret per app+environment, all env vars as keys inside it.

### Operation Pseudocode

```
function executeOperation(intent):
  secret_path  = resolve_path(intent.app_name, intent.environment)
  current      = boto3.get_secret_value(secret_path)
  data         = json.parse(current)

  switch intent.operation:
    case "add":
      if intent.key in data:
        return ask_clarification("Key already exists. Replace or cancel?")
      data[intent.key] = intent.value

    case "update" | "replace" | "append":
      data[intent.key] = intent.value

    case "rename_key":
      if intent.key not in data: return error("Key not found")
      data[intent.new_key] = data[intent.key]
      del data[intent.key]

    case "delete_key":
      if intent.key not in data: return warn("Key not found — nothing deleted")
      del data[intent.key]

  boto3.put_secret_value(secret_path, json.stringify(data))
  db.audit_log.insert(intent, status="success")
  slack.reply_thread(success_message(intent))
```

---

## 7. AWS Infrastructure

### 7.1 ALB Instead of API Gateway

**API Gateway is skipped.** Instead, use an **AWS Application Load Balancer (ALB)** with an HTTPS listener to receive Slack events. If your EKS cluster already has an ALB Ingress Controller (AWS Load Balancer Controller) set up, this costs nothing extra beyond the ALB hourly rate (~$0.008/hr ≈ $6/month), which you likely already pay for other services.

Slack requires a publicly reachable HTTPS URL. The ALB + an ACM certificate (free) satisfies this.

```
Slack → HTTPS POST → ALB (public, port 443, ACM cert)
                       → Target Group → SlackVault EKS Service (ClusterIP)
```

Kubernetes Ingress resource handles routing `/slack/events` to the SlackVault service — no API Gateway, no Lambda, no extra cost.

> **Why not API Gateway?** API Gateway HTTP API costs $1/million requests (cheap at low volume) but adds operational complexity — VPC Links, separate auth layer, separate throttling config. Since you're already on EKS with an ALB, routing through the ALB is simpler and effectively free.

### 7.2 AWS Secrets Manager

- Secrets stored as JSON key-value blobs
- Naming: `slackvault/{env}/{app-name}`
- One additional secret holds SlackVault's own credentials: `slackvault/system/credentials`
- CloudTrail logs all SM API calls automatically

### 7.3 IAM Role (IRSA)

SlackVault uses **IRSA** (IAM Roles for Service Accounts) — the EKS pod gets an IAM role via its Kubernetes service account. No static credentials anywhere.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SecretsAccess",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:DescribeSecret"
      ],
      "Resource": "arn:aws:secretsmanager:{region}:{account-id}:secret:slackvault/*"
    }
  ]
}
```

An explicit `Deny` on anything outside `slackvault/*` ensures a misconfiguration can never touch other secrets.

### 7.4 CloudWatch Logs

- Log group: `/slackvault/service`
- Structured JSON logs from the pod
- Basic alarms: `SecretUpdateFailed > 3 in 5 min` → SNS → email or Slack

---

## 8. EKS Deployment

### Kubernetes Resources (minimal, focused)

```
Namespace:      slackvault
Deployment:     slackvault  (replicas: 2)
Service:        ClusterIP   (exposes port 8000 internally)
Ingress:        routes /slack/events → Service via ALB
ServiceAccount: slackvault-sa  (IRSA annotated)
```

No HPA, PDB, or NetworkPolicy needed at this stage. Two replicas give you basic redundancy. You can add autoscaling later when you have traffic data to base it on. NetworkPolicy adds complexity with limited benefit for an internal tool — skip it.

### ServiceAccount (IRSA)

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: slackvault-sa
  namespace: slackvault
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::{account-id}:role/slackvault-role
```

### Deployment (simplified)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: slackvault
  namespace: slackvault
spec:
  replicas: 2
  selector:
    matchLabels:
      app: slackvault
  template:
    metadata:
      labels:
        app: slackvault
    spec:
      serviceAccountName: slackvault-sa
      containers:
        - name: slackvault
          image: your-ecr-repo/slackvault:latest
          ports:
            - containerPort: 8000
          env:
            - name: APP_SECRET_ARN
              value: "arn:aws:secretsmanager:{region}:{account-id}:secret:slackvault/system/credentials"
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8000
          readinessProbe:
            httpGet:
              path: /readyz
              port: 8000
```

The **only** environment variable injected at the Kubernetes level is `APP_SECRET_ARN` — the ARN of the system credentials secret in AWS Secrets Manager. Everything else is loaded by the app at startup (see Section 9).

---

## 9. Configuration Loading at Startup

### 9.1 Production Mode

On pod startup, SlackVault calls `boto3.get_secret_value(APP_SECRET_ARN)` and loads all configuration into the runtime environment. No ConfigMaps, no Kubernetes Secrets, no Secrets Store CSI Driver needed.

Only `APP_SECRET_ARN` is injected via the Kubernetes Deployment manifest. Every other config value comes from the AWS SM secret.

### 9.2 Local Development Mode

When `APP_SECRET_ARN` is not set, the app loads configuration from a `.env` file via `python-dotenv`. This is the intended workflow for local development and Docker Compose.

The startup sequence:
1. `python-dotenv` loads `.env` (if present, non-overwriting existing env vars)
2. If `APP_SECRET_ARN` is set → fetch all config from AWS Secrets Manager (overrides .env)
3. If `APP_SECRET_ARN` is not set → use env vars / .env values directly

### System Secret in AWS Secrets Manager

Secret name: `slackvault/system/credentials`

```json
{
  "SLACK_SIGNING_SECRET":   "your-slack-signing-secret",
  "SLACK_BOT_TOKEN":        "xoxb-your-bot-token",
  "DEEPSEEK_API_KEY":       "your-deepseek-api-key",
  "DEEPSEEK_API_BASE_URL":  "https://api.deepseek.com",
  "DEEPSEEK_MODEL":         "deepseek-chat",
  "ALLOWED_CHANNEL_IDS":    "C012XY,C034AB",
  "DB_URL":                 "mongodb://user:pass@mongo-host:27017",
  "MONGO_DB_NAME":          "slackvault",
  "TRIGGER_MODE":           "mention",
  "AUTO_CREATE_SECRET":     "true",
  "LOG_LEVEL":              "INFO"
}
```

### Startup Loader (Python)

```python
import boto3, json, os
from dotenv import load_dotenv

def load_config():
    load_dotenv(override=False)
    arn = os.environ.get("APP_SECRET_ARN")
    if not arn:
        # Local dev mode — use .env values directly
        return
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=arn)
    secrets = json.loads(response["SecretString"])
    for key, value in secrets.items():
        os.environ[key] = str(value)

# Called once at app startup, before anything else
load_config()
```

This means:
- **Production**: Only `APP_SECRET_ARN` is in the K8s manifest. All other secrets come from AWS SM at runtime. Rotating any config value requires only a `PutSecretValue` call — optionally combined with a pod restart.
- **Local dev**: Set all config in `.env`, leave `APP_SECRET_ARN` empty. The app uses `.env` values directly.
- No secrets ever live in Kubernetes manifests, ConfigMaps, or environment variable files
- IRSA provides the IAM permission to call `GetSecretValue` on the system ARN at pod startup

---

## 10. Security Model

| Threat | Mitigation |
|---|---|
| Slack webhook spoofing | HMAC-SHA256 signature verified on every request before any processing |
| Replay attack (duplicate Slack events) | Dedup by `event_id` in DB (or in-memory set with TTL) |
| LLM prompt injection via Slack message | Input sanitization + strict JSON schema output validation |
| Accidental prod secret modification | IAM Deny on secrets containing `prod`/`production` + code-level path guard + LLM env validation |
| Secret values exposed in Slack | SlackVault never echoes values — only key names in replies |
| Over-privileged pod | IRSA scoped only to needed SM actions, explicit Deny on `DeleteSecret` |
| Credentials in manifests | Only `APP_SECRET_ARN` (a non-secret ARN) is in the manifest; credentials loaded from SM at runtime |

### Slack Signature Verification

```python
import hmac, hashlib, time

def verify_slack_signature(request):
    timestamp = request.headers["X-Slack-Request-Timestamp"]
    if abs(time.time() - int(timestamp)) > 300:
        raise Exception("Request too old — possible replay")

    sig_base = f"v0:{timestamp}:{request.body}"
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_base.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed, request.headers["X-Slack-Signature"]):
        raise Exception("Invalid signature")
```

---

## 11. Slack Bot Configuration

### Required OAuth Scopes

| Scope | Purpose |
|---|---|
| `channels:history` | Read messages in public channels |
| `groups:history` | Read messages in private channels |
| `chat:write` | Post threaded replies |
| `reactions:write` | Add emoji reactions as acknowledgement |
| `users:read` | Resolve user display names for audit logs |

### Event Subscriptions

Subscribe to `message.channels` (and `message.groups` if using a private channel).

Request URL: `https://{your-alb-domain}/slack/events`

### Trigger Mode

| Mode | Behaviour |
|---|---|
| `passive` | Reads every message; LLM filters irrelevant ones |
| `mention` | Only activates when the bot is `@SlackVault`-mentioned |

**Recommendation:** Use `mention` mode in a busy shared engineering channel. It reduces DeepSeek API calls and avoids false triggers on unrelated messages.

### Slack Reply Examples

**Success:**
```
✅ Done!
  App:          payments-service
  Environment:  stage
  Operation:    replace
  Key:          DB_PASSWORD
  SM Path:      slackvault/stage/payments-service
  Requested by: @john.doe
  Time:         2026-06-05T10:32:44Z
```

**Production rejection:**
```
🚫 Rejected: SlackVault does not manage production secrets.
Use the standard AWS Console runbook for production changes.
```

**Clarification needed:**
```
🤔 Which app should LOG_LEVEL=debug be added to in stage?
Known apps: auth-service, payments-service, api-gateway
```

**Key conflict (add on existing key):**
```
⚠️ DB_HOST already exists in payments-service/stage.
Reply "replace" to overwrite it, or "cancel" to abort.
This request expires in 10 minutes.
```

---

## 12. Audit & Observability

### Audit Table

Every operation (success, failure, rejection) writes a document to `slackvault_audit` in MongoDB. This gives you a full history: who requested what, on which app and environment, when, and whether it succeeded.

### CloudWatch

- Structured JSON logs from the pod → `/slackvault/service` log group
- Alarm: `ERROR` log count > 5 in 5 minutes → SNS → email or Slack webhook

### Health Endpoints

- `GET /healthz` — liveness: returns `{"status": "ok"}` if the service loop is alive
- `GET /readyz` — readiness: pings MongoDB and checks AWS Secrets Manager reachability. Returns `{"status": "ok", "checks": {"mongodb": true, "secrets_manager": true}}` on success, or 503 with partial failures

---

## 13. Error Handling & Edge Cases

| Scenario | Behaviour |
|---|---|
| Slack event delivered twice | Deduplicated by `event_id`; second delivery silently dropped |
| LLM returns malformed JSON | Retry once with stricter prompt; on second failure → "I couldn't parse that, please rephrase" |
| AWS SM throttled | Exponential backoff (3 retries, max 8s delay); CloudWatch alarm if all retries fail |
| AWS SM service error | Retried with exponential backoff (same as throttling) |
| Secret path doesn't exist in SM | Auto-create secret if `AUTO_CREATE_SECRET=true`; reject with explanation if false |
| Production environment requested | Hard reject at validation layer; logged as `status=rejected` |
| Unknown app name | Clarification reply listing registered apps |
| Missing key name | Clarification reply asking for the key |
| DeepSeek API unreachable | Reply "AI service temporarily unavailable, try again shortly" + CloudWatch alarm |
| Bot message in channel | Silently ignored — checked via `bot_id` presence in event payload |
| DB write fails | Log to CloudWatch; still reply success to Slack (SM was already updated); retry DB write async |

---

## 14. Terraform — Risks & Guidance

Terraform is a solid choice for managing the AWS resources SlackVault needs. Here is an honest breakdown of where it helps, where it can hurt, and whether you actually need it.

### What Terraform Would Manage for SlackVault

- IAM role + policy (IRSA)
- ACM certificate (for ALB HTTPS)
- CloudWatch log group + alarms + SNS topic
- `slackvault/system/credentials` secret in SM (structure only — values set manually or via CI)
- ECR repository (for the Docker image)

It does **not** manage your EKS cluster, RDS/MongoDB, or ALB if those already exist — you just reference them by ARN or name.

### Risks to Know Before Using Terraform

**State file is a single point of truth.**
Terraform tracks everything it manages in a state file. If two people run `terraform apply` simultaneously, or if the state file gets corrupted or deleted, you can end up with real infrastructure that Terraform no longer knows about. Fix: store state in S3 + DynamoDB state locking from day one. Never use local state in a team environment.

**`terraform destroy` is destructive and fast.**
Running it in the wrong workspace or against the wrong environment deletes real infrastructure with no extra confirmation. An accidental `terraform destroy` on a production-adjacent account has ended careers. Fix: use workspaces or separate state files per environment, add `lifecycle { prevent_destroy = true }` on critical resources, and restrict who can run `destroy` in CI.

**Drift is painful to reconcile.**
If someone manually changes an IAM policy or a CloudWatch alarm in the Console, Terraform will either overwrite it on next apply or show confusing diffs. Fix: enforce a rule — anything Terraform manages must only be changed via Terraform.

**First apply on existing infrastructure.**
If you import existing resources into Terraform state carelessly, it may try to recreate them. Always run `terraform plan` and read it fully before the first `apply` on any existing resource.

**Secret values and Terraform state.**
If you put secret values (like `SLACK_BOT_TOKEN`) directly in Terraform variables, they get stored in the state file in plaintext. Fix: never put secret values in Terraform variables — create the SM secret structure in Terraform but populate values manually or via a CI step that calls `aws secretsmanager put-secret-value` directly.

### Is Terraform Necessary?

For SlackVault specifically — **no, not at first.** The AWS resources involved are minimal:
- One IAM role + policy → create via AWS Console or a single `aws iam` CLI command
- One CloudWatch log group → auto-created when the first log arrives
- One SNS alarm → 5-minute Console task

Terraform makes sense when you need to replicate the setup across multiple accounts/environments or when you want changes in version control. Start with manual setup, add Terraform later once the system is stable and you know exactly what you need.

---

## 15. Pricing Breakdown

All estimates assume low-to-moderate internal tooling usage: ~5,000 Slack events/month, ~500 SM operations/month.

### AWS Secrets Manager

| Item | Cost |
|---|---|
| Per secret stored | $0.40/month per secret |
| Per 10,000 API calls | $0.05 |
| SlackVault system secret (1) | $0.40/mo |
| App secrets (e.g. 10 apps × 2 envs = 20 secrets) | $8.00/mo |
| API calls (500 get + 500 put/mo) | ~$0.005/mo |
| **SM Total** | **~$8–10/mo** |

> Secrets Manager is the only meaningful AWS cost this system adds.

### ALB (replacing API Gateway)

If you already have an ALB in your EKS cluster (very likely if you use the AWS Load Balancer Controller), adding a new Ingress rule for `/slack/events` costs **$0 extra**. If you need a new ALB:
- ~$0.008/hr = ~$6/mo + $0.008 per LCU (negligible for internal tooling traffic)

API Gateway HTTP API would cost $1.00 per million requests — at 5,000 requests/month that's $0.005/mo, technically cheaper, but adds operational complexity that isn't worth it at this scale.

### DeepSeek API

DeepSeek is significantly cheaper than OpenAI/Anthropic for this use case.

| Model | Input | Output |
|---|---|---|
| `deepseek-chat` | ~$0.14/1M tokens | ~$0.28/1M tokens |

Each Slack message parse uses roughly 500–800 tokens. At 5,000 events/month (most filtered as irrelevant in passive mode, or not triggered in mention mode):

- Mention mode (only actual requests): ~200 LLM calls/mo → **<$0.01/mo**
- Passive mode (every message): ~5,000 LLM calls/mo → **~$0.30–0.50/mo**

DeepSeek is effectively free at this scale.

### EKS Pod

The pod runs on your existing EKS node group — no new EC2 instances needed for a 2-replica lightweight FastAPI service. Additional cost: negligible (small CPU/memory reservation on existing nodes).

### MongoDB

You already have MongoDB. Adding one collection (`slackvault_audit`) with ~500 rows/month of writes is zero meaningful cost.

### CloudWatch

- Log ingestion: $0.50/GB — a low-traffic service generates < 50 MB/month → **<$0.05/mo**
- Alarms: $0.10/alarm/month — 2 alarms → $0.20/mo

### Total Monthly Cost Estimate

| Component | Cost |
|---|---|
| AWS Secrets Manager | ~$8–10/mo |
| ALB (if new) | ~$6/mo |
| DeepSeek API | <$1/mo |
| CloudWatch | ~$0.25/mo |
| EKS pods | $0 (existing nodes) |
| RDS/MongoDB | $0 (existing MongoDB) |
| **Total** | **~$9–17/mo** |

The dominant cost is Secrets Manager storage, driven by how many app secrets you have. If you already have those secrets created, the incremental cost of running SlackVault is essentially the DeepSeek API fees — well under $1/month.

---

## 16. Project Folder Structure

```
slackvault/
├── SLACKVAULT.md                    ← This document
├── docs/
│   └── architecture.md              ← Architecture & data flow documentation
│
├── src/
│   ├── main.py                      ← FastAPI app entry point + readyz health checks
│   ├── startup.py                   ← Config loader (.env for local, AWS SM for prod)
│   ├── slack_handler.py             ← Event routing + signature verification + dedup
│   ├── agent/
│   │   ├── llm_client.py            ← DeepSeek API wrapper
│   │   ├── intent_parser.py         ← Prompt construction + JSON extraction + retry
│   │   ├── intent_validator.py      ← Deterministic validation rules
│   │   └── prompts/
│   │       └── system_prompt.txt    ← Main system prompt + few-shot examples
│   ├── aws/
│   │   └── secrets_manager.py       ← boto3 SM wrapper (get, put, retry/backoff)
│   ├── db/
│   │   ├── base.py                  ← AuditLogger ABC
│   │   └── mongo.py                 ← MongoDB audit logger (motor async + indexes)
│   ├── slack/
│   │   └── responder.py             ← Thread replies + dynamic app registry lookups
│   └── registry/
│       └── app_registry.py          ← App alias → SM path resolver (exact + fuzzy)
│
├── config/
│   └── app-registry.yaml            ← App name aliases + SM path templates
│
├── kubernetes/
│   ├── namespace.yaml
│   ├── serviceaccount.yaml          ← IRSA annotation
│   ├── deployment.yaml              ← APP_SECRET_ARN env var, 2 replicas
│   ├── service.yaml                 ← ClusterIP
│   └── ingress-stg.yaml             ← ALB Ingress → /slack/events
│
├── tests/
│   ├── test_intent_parser.py
│   ├── test_intent_validator.py
│   ├── test_secrets_manager.py
│   ├── test_slack_handler.py
│   ├── test_slack_responder.py
│   ├── test_mongo_audit.py
│   ├── test_app_registry.py
│   └── fixtures/
│       └── sample_messages.json     ← Test Slack message corpus
│
├── Dockerfile
├── docker-compose.yml               ← Local dev (MongoDB + env vars)
├── requirements.txt
└── .env.example                     ← Local dev config template (only APP_SECRET_ARN needed in prod)
```

---

## 17. Implementation Roadmap

### Phase 1 — Core Agent (Week 1–2) ✅ DONE

- [x] Scaffold FastAPI service with `/slack/events`, `/healthz`, `/readyz`
- [x] Implement `startup.py` — dual-mode config loader (.env for local, AWS SM for prod)
- [x] Implement Slack signature verification
- [x] Integrate DeepSeek with system prompt + few-shot examples
- [x] Implement intent validation layer (env guard, op enum, field checks)
- [x] Build SM client for all operations (`add`, `update`, `replace`, `append`, `rename_key`, `delete_key`)
- [x] Write audit rows to MongoDB (motor async driver + auto-created indexes)
- [x] Basic Slack thread replies (success, rejection, clarification, conflict, error)
- [x] Remove Postgres dependency — MongoDB only
- [x] Populated `__init__.py` files with `__all__` exports

### Phase 2 — Full Operations & Robustness (Week 3) ✅ DONE

- [x] All 6 secret operations implemented in SM client
- [x] App registry YAML + fuzzy name matching (Levenshtein ≤ 2)
- [x] Environment substitution in resolved path (`{environment}` → `dev`/`stage`)
- [x] Event deduplication (in-memory set on `event_id`)
- [x] Conflict detection for `add` on existing key
- [x] Bot user ID resolution at startup via Slack `auth.test`
- [x] Strip `@SlackVault` mention prefix before LLM parse
- [x] Dynamic known-apps listing in clarification replies (via AppRegistry injection)
- [x] SM client retry with exponential backoff on throttling/service errors
- [x] Ready readiness probe (`/readyz`) pings MongoDB + AWS SM
- [x] MongoDB indexes auto-created on connect

### Phase 3 — EKS Deployment (Week 4)

- [ ] Docker Compose local dev with MongoDB service
- [ ] Containerize with Docker + push to ECR
- [ ] Configure IRSA on the EKS service account
- [ ] Create `slackvault/system/credentials` secret in AWS SM with all config values
- [ ] Deploy to EKS, configure Slack app Request URL to ALB endpoint
- [ ] End-to-end smoke test with real Slack messages
- [ ] CloudWatch alarms setup (`SecretUpdateFailed > 3 in 5 min` → SNS)
- [ ] Load testing and rate limit configuration

---

## 18. QPS & Concurrency Model

### Capacity Limits

| Layer | Limit | Unit | How to Scale |
|---|---|---|---|
| Pod replicas | 3 | Pods | Increase `replicas` in deployment.yaml |
| Concurrency per pod | 10 (configurable via `MAX_CONCURRENT_OPS`) | Event pipelines | Increase env var per pod |
| **Total concurrent events** | **30** | Simultaneous pipelines | `replicas × MAX_CONCURRENT_OPS` |
| DeepSeek API throughput | ~10 req/s peak at 30 concurrency | Requests/sec | Increase `MAX_CONCURRENT_OPS` |
| AWS SM API throughput | ~50 ops/s (read+write) | Operations/sec | Limited by boto3 + network |
| Per-secret lock | 1 operation at a time | Writes per path | N/A — serialization is intentional |
| Event dedup set | 10,000 entries | In-memory capacity | Auto-clears |

### Throughput

| Scenario | Rate | How |
|---|---|---|
| **New requests (burst)** | 10 QPS per pod | ConcurrencyLimiter with 10 slots |
| **Successful round-trips (with user confirmation)** | ~0.3 ops/s sustained | Each round trip: request (~2s) + think time (~5s) + execute (~1s) |
| **Execution only (no LLM)** | ~50 ops/s | 3 pods × 10 slots / 0.6s per SM read+write |
| **Concurrent confirmations** | Up to 30 parallel | All 30 slots can be executing confirmed operations simultaneously |

### Request Distribution

Slack sends events to the ALB, which distributes across the 3 pod replicas. Each pod independently:

1. Receives the event via ALB round-robin
2. Acquires a semaphore slot (10 per pod)
3. Processes the full pipeline
4. Releases the semaphore slot

Since pods don't share semaphore state, one pod can be at capacity while others are idle. The ALB has no awareness of pod capacity. To avoid imbalance, keep `MAX_CONCURRENT_OPS` per pod equal.

### Resource Consumption (per event)

| Resource | Consumption | Notes |
|---|---|---|
| CPU | ~50ms (mostly SM API I/O wait) | `asyncio.to_thread` pushes sync SM calls to thread pool |
| Memory | ~2MB per active pipeline | Mainly JSON blobs from SM secrets (can be large) |
| Network | ~100KB per event | Slack payload + LLM response + SM data |
| MongoDB writes | 1 per event pipeline | One insert to `slackvault_audit` |

With 30 concurrent events: ~60MB memory, ~3MB network buffer, 30 MongoDB writes.

---

*SlackVault — Because updating secrets should be a Slack message, not a war story.*
