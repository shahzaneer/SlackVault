# SlackVault — Stage Testing Guide

## Overview

Stage testing validates SlackVault in a production-like environment on EKS with IRSA, an ALB, MongoDB Atlas, and real Slack integration. The stage AWS account should be separate from production but can share infrastructure patterns.

**Goal:** Validate full EKS deployment, IAM security, multi-replica operation, and cross-pod consistency before any production use.

---

## Architecture (Stage)

```
Slack → ALB (internet-facing, ACM cert)
         → SlackVault EKS Service (ClusterIP)
              → Pod replica 1 (FastAPI)
              → Pod replica 2 (FastAPI)
              → Pod replica 3 (FastAPI)
                   ├── AWS SM (stage AWS account, via IRSA)
                   ├── MongoDB Atlas (dedicated cluster)
                   └── DeepSeek API
```

---

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| `kubectl` | >= 1.28 | Deploy to EKS |
| `aws` CLI | >= 2 | Create secrets, IAM, ECR |
| `docker` | >= 24 | Build image |
| `helm` (optional) | >= 3 | AWS Load Balancer Controller |

**Stage AWS account:** Separate from production. No overlap with prod secrets or resources.

**EKS cluster:** Must exist. SlackVault will be deployed into it via manifests.

---

## Infrastructure Setup

### 1. Create a Stage Slack App

Go to https://api.slack.com/apps → **Create New App** → From scratch.

| Setting | Value |
|---|---|
| App Name | `SlackVault-Stage` |
| Workspace | Your engineering workspace (same as sandbox, but different channel) |

**OAuth Scopes** (Bot Token Scopes):
```
channels:history
groups:history
chat:write
reactions:write
users:read
```

**Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-...`).

**Event Subscriptions** → Enable → Request URL temporarily as `https://placeholder.example/slack/events` (update after ALB provisions).

Subscribe to bot events:
```
message.channels
message.groups
```

**Basic Information** → copy the **Signing Secret**.

### 2. Create MongoDB Atlas Cluster

1. Go to https://cloud.mongodb.com → Create a free M0 cluster
2. Create a database user: username `slackvault`, generate a password
3. Add your VPC CIDR or `0.0.0.0/0` to Network Access (if IP-based)
4. Click **Connect** → **Drivers** → copy connection string:
   ```
   mongodb+srv://slackvault:<password>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
   ```

### 3. Create AWS Secrets Manager System Secret

This secret holds all runtime config for SlackVault. Create it in the **stage AWS account**:

```bash
aws secretsmanager create-secret \
    --name "slackvault/system/credentials-stage" \
    --description "SlackVault stage runtime configuration" \
    --secret-string '{
        "SLACK_SIGNING_SECRET": "your-stage-slack-signing-secret",
        "SLACK_BOT_TOKEN": "xoxb-your-stage-bot-token",
        "DEEPSEEK_API_KEY": "sk-your-deepseek-api-key",
        "DEEPSEEK_API_BASE_URL": "https://api.deepseek.com",
        "DEEPSEEK_MODEL": "deepseek-chat",
        "ALLOWED_CHANNEL_IDS": "C012XY345",
        "DB_URL": "mongodb+srv://slackvault:password@cluster0.xxxxx.mongodb.net/slackvault",
        "MONGO_DB_NAME": "slackvault",
        "TRIGGER_MODE": "mention",
        "AUTO_CREATE_SECRET": "true",
        "MAX_CONCURRENT_OPS": "10",
        "LOG_LEVEL": "INFO"
    }' \
    --region us-east-1
```

Note the ARN — you'll use it as `APP_SECRET_ARN` in the Deployment manifest.

### 4. Create Test App Secrets

Create stage app secrets. These are the actual secrets SlackVault will operate on:

```bash
# Payments service
aws secretsmanager create-secret \
    --name "dev/payments-service" \
    --secret-string '{"DB_HOST":"localhost","DB_PASSWORD":"stage123"}' \
    --region us-east-1

aws secretsmanager create-secret \
    --name "stage/payments-service" \
    --secret-string '{"DB_HOST":"stage.db.internal","DB_PASSWORD":"stage-pass"}' \
    --region us-east-1

# Auth service
aws secretsmanager create-secret \
    --name "dev/auth-service" \
    --secret-string '{"AUTH_URL":"http://auth.dev","TOKEN":"dev-token"}' \
    --region us-east-1

aws secretsmanager create-secret \
    --name "stage/auth-service" \
    --secret-string '{"AUTH_URL":"http://auth.stage","TOKEN":"stage-token"}' \
    --region us-east-1
```

Verify:
```bash
aws secretsmanager list-secrets --region us-east-1 | jq '.SecretList[].Name'
```

### 5. Create IAM Role for IRSA

**5a. Create the IAM policy (`stage-slackvault-policy.json`):**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SlackVaultSecretsAccess",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:DescribeSecret",
        "secretsmanager:CreateSecret",
        "secretsmanager:ListSecrets"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DenyDeleteSecret",
      "Effect": "Deny",
      "Action": ["secretsmanager:DeleteSecret"],
      "Resource": "*"
    },
    {
      "Sid": "DenyProdSecrets",
      "Effect": "Deny",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:DescribeSecret",
        "secretsmanager:CreateSecret"
      ],
      "Resource": [
        "arn:aws:secretsmanager:*:*:secret:*prod*",
        "arn:aws:secretsmanager:*:*:secret:*production*"
      ]
    }
  ]
}
```

```bash
aws iam create-policy \
    --policy-name slackvault-stage-sm-policy \
    --policy-document file://stage-slackvault-policy.json
```

**5b. Create the IAM role with EKS trust:**

```bash
# Get your EKS cluster's OIDC provider URL
OIDC_PROVIDER=$(aws eks describe-cluster --name your-stage-cluster \
    --query "cluster.identity.oidc.issuer" --output text | sed 's|https://||')

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

cat > stage-slackvault-trust.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_PROVIDER}"
    },
    "Condition": {
      "StringEquals": {
        "${OIDC_PROVIDER}:sub": "system:serviceaccount:slackvault:slackvault-sa"
      }
    }
  }]
}
EOF

aws iam create-role \
    --role-name slackvault-stage-role \
    --assume-role-policy-document file://stage-slackvault-trust.json

aws iam attach-role-policy \
    --role-name slackvault-stage-role \
    --policy-arn arn:aws:iam::${ACCOUNT_ID}:policy/slackvault-stage-sm-policy
```

### 6. Build and Push Docker Image to ECR

```bash
# Login to ECR
aws ecr get-login-password --region us-east-1 | \
    docker login --username AWS --password-stdin \
    ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com

# Create ECR repo (one-time)
aws ecr create-repository --repository-name slackvault-stage --region us-east-1

# Build and push
docker build -t slackvault-stage .
docker tag slackvault-stage:latest \
    ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/slackvault-stage:latest
docker push ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/slackvault-stage:latest
```

### 7. Fill and Apply Kubernetes Manifests

Replace all placeholders in `kubernetes/*.yaml`:

```bash
REGION="us-east-1"
APP="slackvault-stage"
NS="slackvault"
REPO="slackvault-stage"
TAG="latest"

for f in kubernetes/*.yaml; do
    sed -i '' "s/_APP_NAME/${APP}/g; s/_NAMESPACE/${NS}/g; s/_AWS_ACCOUNT_ID/${ACCOUNT_ID}/g; s/_AWS_REGION/${REGION}/g; s/_ECR_REPOSITORY/${REPO}/g; s/_IMAGE_TAG/${TAG}/g" "$f"
done
```

**Also update `kubernetes/deployment.yaml`:** replace the `APP_SECRET_ARN` value with the ARN of the system secret you created in step 3:

```yaml
- name: APP_SECRET_ARN
  value: "arn:aws:secretsmanager:us-east-1:${ACCOUNT_ID}:secret:slackvault/system/credentials-stage-xxxxxx"
```

**Apply manifests:**

```bash
kubectl apply -f kubernetes/namespace.yaml
kubectl apply -f kubernetes/serviceaccount.yaml
kubectl apply -f kubernetes/deployment.yaml
kubectl apply -f kubernetes/service.yaml
kubectl apply -f kubernetes/ingress-stg.yaml
```

### 8. Verify Deployment

```bash
# Wait for pods
kubectl get pods -n slackvault -w
# Should show 3/3 Running

# Check logs
kubectl logs -n slackvault -l app=slackvault-stage --tail=50
# Should show:
#   Connected to MongoDB
#   Discovered X secrets from AWS SM
#   Resolved bot user_id: B...

# Check health
ALB_URL=$(kubectl get ingress -n slackvault -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}')
curl -k https://${ALB_URL}/healthz
# {"status": "ok"}

curl -k https://${ALB_URL}/readyz
# {"status": "ok", "checks": {"mongodb": true, "secrets_manager": true}}

curl -k https://${ALB_URL}/stats
# {"pending_confirmations": 0, "active_locks": 0, ...}
```

### 9. Update Slack App with ALB URL

Go to your Slack App → **Event Subscriptions** → set Request URL to:

```
https://${ALB_URL}/slack/events
```

Wait for **Verified** status.

---

## Test Scenarios

All test scenarios from `sandbox-testing.md` apply here as well. Additionally, test stage-specific scenarios:

### Test S1: Multi-replica operation

1. Open 3 terminals
2. Each terminal runs: `kubectl logs -n slackvault -l app=slackvault-stage -f`
3. In Slack, send the same request twice rapidly:
   ```
   @SlackVault-Stage add CONCUR_TEST=value1 to payments in dev
   @SlackVault-Stage add CONCUR_TEST=value2 to payments in dev
   ```
4. Confirm both
5. Only one should succeed (the second gets conflict). Verify the final value is the one that was written second.

### Test S2: Pod restart survival

```bash
# Create a pending confirmation
# Send: @SlackVault-Stage add RESTART_TEST=survive to payments in dev

# Kill the pod that's processing
kubectl delete pod -n slackvault -l app=slackvault-stage --force

# Wait for new pod
kubectl get pods -n slackvault -w

# Reply "yes" to the confirmation prompt
# Expected: Operation should still execute (MongoDB-backed)
```

### Test S3: Cross-replica consistency

```bash
# While 3 pods are running, identify each pod
kubectl get pods -n slackvault

# Delete the secret in AWS SM directly
aws secretsmanager put-secret-value \
    --secret-id dev/payments-service \
    --secret-string '{"DIRECT_UPDATE":"done outside slackvault"}'

# Now update it via SlackVault
@SlackVault-Stage add CROSS_POD_TEST=works to payments in dev
# Confirm
# Expected: SlackVault re-reads the secret (includes DIRECT_UPDATE), appends CROSS_POD_TEST
```

### Test S4: Lazy refresh on new secret

1. Create a new secret directly in AWS SM:
   ```bash
   aws secretsmanager create-secret \
       --name "stage/new-service" \
       --secret-string '{"KEY":"val"}' \
       --region us-east-1
   ```

2. Ask SlackVault to operate on the new app:
   ```
   @SlackVault-Stage add MY_KEY=myval to new-service in stage
   ```

3. **Expected:** After the "unknown app" path, SlackVault should discover the new secret via lazy refresh and proceed. If it happens within 30s of a previous refresh, wait 30s and retry.

### Test S5: Stage environment only

```
@SlackVault-Stage add KEY=val to payments in stage
```

Should work — `stage` is a valid environment. Same test with `staging`, `stg`.

### Test S6: Load test (burst)

Send 10 requests simultaneously (from different users or same user rapidly):

```
@SlackVault-Stage add BURST_KEY_1=val to payments in dev
@SlackVault-Stage add BURST_KEY_2=val to auth in dev
...
```

Check `/stats` during the burst — `available_concurrency_slots` should drop to 0 and recover.

---

## Observability

| Endpoint | What to check |
|---|---|
| `GET /healthz` | Should always return 200 |
| `GET /readyz` | Both `mongodb` and `secrets_manager` should be `true` |
| `GET /stats` | Monitor `pending_confirmations`, `available_concurrency_slots` during load |
| CloudWatch Logs | Log group `/slackvault/service` in the stage account |

---

## Rollback Plan

If stage testing reveals issues:

```bash
# Scale down to 0
kubectl scale deployment slackvault-stage -n slackvault --replicas=0

# Or delete everything
kubectl delete namespace slackvault
```

The app secrets in AWS SM are **not** affected by SlackVault operations (each operation writes to SM). If you need to revert a specific change:

```bash
# Restore a previous version of the secret
aws secretsmanager get-secret-value \
    --secret-id dev/payments-service \
    --version-id <previous-version-id> \
    --query SecretString --output text
```

---

## Cleanup

After stage testing:

```bash
# Delete EKS resources
kubectl delete namespace slackvault

# Delete ECR image
aws ecr delete-repository --repository-name slackvault-stage --force

# Delete IAM role + policy
aws iam detach-role-policy --role-name slackvault-stage-role \
    --policy-arn arn:aws:iam::${ACCOUNT_ID}:policy/slackvault-stage-sm-policy
aws iam delete-role --role-name slackvault-stage-role
aws iam delete-policy --policy-name slackvault-stage-sm-policy

# Delete test secrets
aws secretsmanager delete-secret --secret-id dev/payments-service --force-delete-without-recovery
aws secretsmanager delete-secret --secret-id stage/payments-service --force-delete-without-recovery
aws secretsmanager delete-secret --secret-id dev/auth-service --force-delete-without-recovery
aws secretsmanager delete-secret --secret-id stage/auth-service --force-delete-without-recovery

# Delete the system secret
aws secretsmanager delete-secret --secret-id slackvault/system/credentials-stage --force-delete-without-recovery

# Delete Slack app from https://api.slack.com/apps
```

---

## Promotion to Production

After stage testing passes all scenarios:

1. Create a production-specific Slack app with restricted channel access
2. Create a separate MongoDB Atlas cluster for production
3. Use a production AWS account with the same IRSA pattern
4. Deploy from the same Docker image (already tested in stage)
5. Enable CloudWatch alarms before production deployment
6. Restrict `ALLOWED_CHANNEL_IDS` to a single, controlled channel