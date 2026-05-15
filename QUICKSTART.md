# Quickstart

Get einkgen running in your own AWS account. The first half (Part 1) is the
short list of things only a human can do — accounts, tooling, secrets. The
second half (Parts 2–4) is a runbook designed for an AI coding agent to
execute, but a human can follow it just as well.

For *what* you're deploying, see [ARCHITECTURE.md](ARCHITECTURE.md).
If you're an agent reading this, also read [CLAUDE.md](CLAUDE.md) for
the hard rules (cost, secrets, destructive-op gates).

---

## Part 1 — Human prerequisites (do these first)

Front-loaded so you can do them once, walk away, and hand the rest to an
agent.

### 1.1. Accounts

- **AWS account** with admin (or at least: IAM, S3, CloudFront, Lambda,
  API Gateway, Secrets Manager, EventBridge, CloudWatch). Note the
  12-digit account ID — you'll paste it once.
- **OpenAI API key.** Create one at
  <https://platform.openai.com/api-keys>. Copy the `sk-...` value to your
  password manager. The agent will paste it into Secrets Manager on your
  behalf; it never lives in source.

### 1.2. Local tooling

| Tool | Why | Check |
| --- | --- | --- |
| Python 3.11+ | CLI + Lambda runtime parity | `python3 --version` |
| Node.js 20+ | CDK + Vite | `node --version` |
| AWS CLI v2 | profile config, secret writes | `aws --version` |
| Docker (running) | CDK bundles Lambdas in a Python container | `docker info` |

On macOS: `brew install python node awscli` and install Docker Desktop.
On Linux: distro packages or `nvm`/`pyenv` for version pinning.

### 1.3. Clone

```sh
git clone https://github.com/<your-fork>/einkgen.git
cd einkgen
```

### 1.4. Configure an AWS profile

The project defaults to a profile named `einkgen` and the `us-east-1`
region. Pick whatever you like — the agent runbook will respect
`AWS_PROFILE` and `AWS_REGION`.

```sh
aws configure --profile einkgen
# AWS Access Key ID:     <yours>
# AWS Secret Access Key: <yours>
# Default region name:   us-east-1
# Default output format: json

# Sanity check — should print your account ID:
AWS_PROFILE=einkgen aws sts get-caller-identity
```

### 1.5. Pick a device-status token

This is the shared secret the Inkplate firmware will send in every status
POST. Generate one now; the agent will store it in Secrets Manager and you
will later bake the same value into
`firmware/inkplate10/secrets.h` when the device arrives.

```sh
openssl rand -hex 32
```

Save the value somewhere private. Don't paste it into chat or commit it
anywhere.

### 1.6. (One-time) CDK bootstrap

If you've never used CDK in this account+region, you'll need to bootstrap
the CDK toolkit stack once. The agent can do it for you in step 3.3, or
you can run it now:

```sh
cd infra && npm install
AWS_PROFILE=einkgen AWS_REGION=us-east-1 npx cdk bootstrap
cd ..
```

That's it for human steps. Hand the rest to an agent.

---

## Part 2 — Hand-off prompt for an agent

Paste something like the following into Claude Code (or whichever agent
you use) inside the repo root:

> Deploy einkgen to my AWS account by following
> [QUICKSTART.md](QUICKSTART.md) Part 3 step by step. I have already done
> Part 1. Settings:
>
> - `AWS_PROFILE=einkgen`
> - `AWS_REGION=us-east-1`
> - Environment name: `dev`
> - OpenAI API key: (I'll paste when you ask)
> - Device-status token: (I'll paste when you ask)
>
> Run each command, summarise the output, and only proceed if the previous
> step succeeded. Don't print secret values back at me — show length or a
> SHA-256 prefix instead. Stop and ask if anything looks unexpected.

The agent needs permission to run `aws`, `npx cdk`, `npm`, and read/write
files in the repo. Standing approval for `cdk deploy` to *your* account
makes the runbook smoother.

---

## Part 3 — Agent deploy runbook

All commands assume `AWS_PROFILE=einkgen AWS_REGION=us-east-1` (override
via env vars). All paths are relative to the repo root.

### 3.1. Sanity-check the environment

```sh
AWS_PROFILE=einkgen AWS_REGION=us-east-1 aws sts get-caller-identity
node --version       # >= 20
python3 --version    # >= 3.11
docker info >/dev/null && echo "docker ok"
```

If `docker info` fails, ask the human to start Docker Desktop. CDK Lambda
bundling needs a running daemon.

### 3.2. Install JS dependencies

```sh
( cd infra && npm install )
( cd web   && npm install )
```

### 3.3. Bootstrap CDK (idempotent)

Safe to run even if already bootstrapped:

```sh
( cd infra && AWS_PROFILE=einkgen AWS_REGION=us-east-1 npx cdk bootstrap )
```

### 3.4. First deploy — infrastructure only (no web assets yet)

The web SPA needs the deployed API/CDN URLs at build time, so the first
deploy goes out without `web/`. `includeWebAssets` defaults to `false`.

```sh
( cd infra && AWS_PROFILE=einkgen AWS_REGION=us-east-1 npx cdk deploy \
    --outputs-file cdk-outputs.json \
    --require-approval never \
    -c env=dev )
```

After it finishes, `infra/cdk-outputs.json` contains the URLs you need:

```sh
jq '.["EinkgenStack-dev"]' infra/cdk-outputs.json
```

Expected keys: `BucketName`, `CdnDomain`, `CdnDistributionId`,
`ReadApiUrl`, `DeviceStatusUrl`, `OpenAiSecretName`,
`DeviceStatusSecretName`.

### 3.5. Populate the secrets

The stack created both secrets with placeholder strings
(`REPLACE_ME_POST_DEPLOY`). Overwrite them with the values the human gave
you:

```sh
AWS_PROFILE=einkgen AWS_REGION=us-east-1 aws secretsmanager put-secret-value \
  --secret-id einkgen/openai_api_key \
  --secret-string "<OPENAI_API_KEY>"

AWS_PROFILE=einkgen AWS_REGION=us-east-1 aws secretsmanager put-secret-value \
  --secret-id einkgen/device_status_token \
  --secret-string "<DEVICE_STATUS_TOKEN>"
```

Verify *length only* so you don't echo secrets:

```sh
AWS_PROFILE=einkgen AWS_REGION=us-east-1 aws secretsmanager get-secret-value \
  --secret-id einkgen/openai_api_key --query SecretString --output text \
  | wc -c
# expect: ~50 chars for an OpenAI key

AWS_PROFILE=einkgen AWS_REGION=us-east-1 aws secretsmanager get-secret-value \
  --secret-id einkgen/device_status_token --query SecretString --output text \
  | wc -c
# expect: 64 chars for a hex32 token + 1 newline = 65
```

### 3.6. Build the web SPA against the deployed URLs

Read the URLs from `infra/cdk-outputs.json` and write `web/.env.production`
(gitignored):

```sh
READ_API_URL=$(jq -r '.["EinkgenStack-dev"].ReadApiUrl'  infra/cdk-outputs.json)
CDN_DOMAIN=$(jq -r '.["EinkgenStack-dev"].CdnDomain'     infra/cdk-outputs.json)

cat > web/.env.production <<EOF
VITE_READ_API_URL=${READ_API_URL}
VITE_CDN_BASE=https://${CDN_DOMAIN}
EOF

( cd web && npm run build )
```

This emits `web/dist/`.

### 3.7. Redeploy with the web assets

```sh
( cd infra && AWS_PROFILE=einkgen AWS_REGION=us-east-1 npx cdk deploy \
    --outputs-file cdk-outputs.json \
    --require-approval never \
    -c env=dev -c includeWebAssets=true )
```

This uploads `web/dist/*` to `s3://einkgen-dev/web/` and invalidates
`/web/*` in CloudFront.

### 3.8. Smoke test

```sh
READ_API_URL=$(jq -r '.["EinkgenStack-dev"].ReadApiUrl'      infra/cdk-outputs.json)
DEVICE_URL=$(jq   -r '.["EinkgenStack-dev"].DeviceStatusUrl' infra/cdk-outputs.json)
CDN_DOMAIN=$(jq   -r '.["EinkgenStack-dev"].CdnDomain'       infra/cdk-outputs.json)

# Read API should respond 200 with an empty queue.
curl -sS "${READ_API_URL}/queue"

# Device-status should reject unauthenticated POSTs.
curl -sS -o /dev/null -w "no token   -> %{http_code}\n" \
  -X POST "${DEVICE_URL}/" -H 'Content-Type: application/json' -d '{}'

# CloudFront should serve the SPA index.html.
curl -sS -I "https://${CDN_DOMAIN}/" | head -3
```

Open `https://<CdnDomain>/` in a browser. You should see the three-tab
SPA with `Queue is empty.`, `Device has not reported yet.`, and an empty
History grid. Within 2 hours the EventBridge cron will tick and you'll
see your first frame appear.

### 3.9. (Optional) Enqueue a test prompt now

If the human wants a frame immediately without waiting for cron, install
the Python package and enqueue one:

```sh
pip install -e .

EINKGEN_BUCKET=einkgen-dev \
AWS_PROFILE=einkgen AWS_REGION=us-east-1 \
einkgen queue prompt "Geometric composition. Overlapping circles, squares, triangles in bold flat shapes with high contrast."
```

The generator Lambda will pick it up within a few seconds. Tail logs to
watch the ~55 s run:

```sh
AWS_PROFILE=einkgen AWS_REGION=us-east-1 \
  aws logs tail /aws/lambda/einkgen-generator --follow
```

When the run completes, refresh the SPA's History tab. The new tile
appears at the top.

---

## Part 4 — Day-2 ops

| Task | Command |
| --- | --- |
| Enqueue a prompt | `einkgen queue prompt "<text>"` |
| Enqueue an image | `einkgen queue image <path>` |
| List the queue | `einkgen queue ls` |
| Cancel an item | `einkgen queue rm <id>` |
| Tail generator logs | `aws logs tail /aws/lambda/einkgen-generator --follow` |
| 24h ERROR sweep | `AWS_PROFILE=einkgen ./infra/scripts/check-errors.sh` |
| Rotate device token | `aws secretsmanager put-secret-value …` + reflash `secrets.h` |
| Deploy a code change | re-run §3.6 (web) and/or §3.7 (deploy) |
| Tear it all down | `( cd infra && npx cdk destroy -c env=dev )` |

`einkgen` CLI environment variables:

```
OPENAI_API_KEY=sk-...           # for `einkgen local generate/preview`
AWS_PROFILE=einkgen             # picked up by boto3
EINKGEN_BUCKET=einkgen-dev      # required for `einkgen queue *`
EINKGEN_CDN_BASE=https://…      # used to build manifest image_url
EINKGEN_CF_DISTRIBUTION_ID=…    # optional; enables CF invalidation on publish
```

`.env.example` documents the same set.

---

## Troubleshooting

- **`Cannot find module 'aws-cdk-lib'`** — run `npm install` inside
  `infra/`.
- **Docker daemon not running** — CDK Lambda bundling needs Docker.
  Start Docker Desktop or set `CDK_DOCKER` to a podman wrapper.
- **`Reserved concurrency cannot be greater than the account's available
  concurrency`** — your account's unreserved concurrency pool is too low.
  Either raise the account limit or lower the value in
  [infra/lib/lambdas.ts](infra/lib/lambdas.ts).
- **Web SPA loads but data tabs say "Could not load"** — the
  `VITE_READ_API_URL` baked into the build is stale or wrong. Re-run §3.6
  with the current outputs and §3.7 to redeploy.
- **`/status` returns 404 in the browser console** — expected before any
  device has reported. The SPA handles it (shows "Device has not reported
  yet."); the 404 disappears once `status/device-default.json` exists.
- **Generator Lambda 401s on OpenAI** — the secret wasn't populated.
  Re-run §3.5 and trigger another invocation
  (`einkgen queue prompt "..."` or wait for cron).
