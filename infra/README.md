# einkgen infra (AWS CDK, TypeScript)

Single CDK stack provisioning everything in README §9: one S3 bucket, one
CloudFront distribution, three Lambdas (generator, read-api, device-status),
two Secrets Manager secrets, an EventBridge cron rule, log-group retention,
CloudWatch dashboard, and an ERROR metric filter per Lambda.

## Prerequisites

- Node.js 20+
- Docker (used by CDK Lambda bundling for `pip install` inside the official
  Python 3.12 image).
- AWS credentials with permission to bootstrap and deploy a CDK stack
  (run once: `npx cdk bootstrap`).

## Layout

```
infra/
├── bin/einkgen.ts             CDK app entry
├── lib/
│   ├── einkgen-stack.ts       Top-level stack wiring
│   ├── bucket.ts              S3 (versioning off, SSE-S3, public access blocked)
│   ├── cloudfront.ts          One distribution, OAC, SPA fallback for /web/*
│   ├── secrets.ts             openai_api_key, device_status_token
│   ├── lambdas.ts             3 Lambdas + triggers + Function URLs
│   └── observability.ts       Log retention, ERROR metric filters, dashboard
├── lambda/
│   ├── requirements-generator.txt
│   ├── requirements-read-api.txt
│   ├── requirements-device-status.txt
│   └── _src/                  staged copy of src/einkgen/ (generated at synth)
├── scripts/check-errors.sh    Milestone 12 manual error-check pass
└── cdk.json
```

## Usage

```sh
cd infra
npm install
npx cdk synth                          # full synth (requires web/dist/)
npx cdk synth -c includeWebAssets=false   # skip web BucketDeployment
npx cdk deploy -c env=dev
```

The `env` context value drives the bucket name (`einkgen-<env>`) and the
stack id (`EinkgenStack-<env>`). Default `dev`.

## Post-deploy: populate secrets

The stack creates secrets with placeholder strings (`REPLACE_ME_POST_DEPLOY`).
Overwrite them once:

```sh
aws secretsmanager put-secret-value \
  --secret-id einkgen/openai_api_key \
  --secret-string "sk-..."

aws secretsmanager put-secret-value \
  --secret-id einkgen/device_status_token \
  --secret-string "$(openssl rand -hex 32)"
```

Update `firmware/inkplate10/secrets.h` (gitignored) with the device-status
token so the Inkplate sends a matching `X-Device-Token` header.

## Pillow Lambda layer (Klayers)

Pillow is pulled in as a published Lambda layer from the
[Klayers](https://github.com/keithrozario/Klayers) project rather than being
bundled into the function zip. Reasons:

1. The wheel is large and platform-specific (manylinux x86_64); shipping it
   through `pip install -t` from a macOS dev machine produces a broken layer.
2. Klayers publishes a versioned ARN per region for every minor Pillow
   release, so we always get a vetted, layer-compatible build.

Default ARN, baked into `cdk.json` under `pillowLayerArn`:

```
arn:aws:lambda:us-east-1:770693421928:layer:Klayers-p312-Pillow:16
```

To bump:

1. Browse <https://api.klayers.cloud/api/v2/p3.12/layers/latest/us-east-1/json>
   and find the entry for `Pillow`. The JSON includes the current ARN with
   its latest version suffix.
2. Override at synth/deploy time, or update the value in `cdk.json`:

   ```sh
   npx cdk deploy -c pillowLayerArn='arn:aws:lambda:us-east-1:770693421928:layer:Klayers-p312-Pillow:17'
   ```

Klayers publishes to most commercial AWS regions; pick the matching ARN for
your deploy region.

## Lambda bundling

`lib/lambdas.ts` stages `src/einkgen/` into `infra/lambda/_src/einkgen/` at
synth time (a plain recursive copy via `node:fs`) and uses
`lambda.Code.fromAsset('infra/lambda', { bundling: ... })`. The bundling
container runs:

```
pip install --no-cache-dir -r /asset-input/requirements-<lambda>.txt -t /asset-output
cp -r /asset-input/_src/einkgen /asset-output/einkgen
```

per Lambda. Three independent assets so each function gets only what it
needs (read-api and device-status don't carry Pillow/openai).

When `src/einkgen/` is missing (as in the parallel-track worktrees) the
stager writes a stub `einkgen/__init__.py` so `cdk synth` still succeeds —
intended only for synth smoke-tests. Real deploys must run from a tree
where `src/einkgen/` exists.

## CORS notes

- `einkgen-read-api` is pinned to the CloudFront distribution domain
  (`https://<dxxxx>.cloudfront.net`). There is no circular dependency since
  the Function URL is not served behind CloudFront — the web app talks to
  the Function URL directly. If you front the read-api behind CloudFront in
  the future, switch the Lambda allowedOrigins to `["*"]` (logged here as
  a TODO) before that change to avoid a chicken-and-egg.
- `einkgen-device-status` uses `*` because only firmware POSTs there; no
  browser is in the loop.

## S3 event filter

`addEventNotification(OBJECT_CREATED, ..., { prefix: 'queue/', suffix: '.json' })`.
S3 supports exactly one (prefix, suffix) pair per notification rule, which
is sufficient here: the prefix `queue/` selects the queue, the suffix
`.json` excludes JPEG/PNG uploads under `queue/staged/`. The generator
Lambda is therefore never invoked for staged image uploads.

## CloudWatch (Milestone 12)

- Lambda log retention: 14 days (set on the `Function` itself).
- `infra/lib/observability.ts` adds a `MetricFilter` per log group on the
  literal token `ERROR`, namespace `einkgen`, dimension `Lambda`. The
  resulting metric is plotted on a CloudWatch dashboard (`einkgen-<env>`)
  alongside invocations/errors and duration p50/p99.
- `infra/scripts/check-errors.sh` runs CloudWatch Logs Insights queries
  for the last 24 h (override with `./check-errors.sh 6h`) across all
  three Lambdas. Run this manually until SNS alerting lands (Milestone 13+).

## Troubleshooting

- **`Cannot find module 'aws-cdk-lib'`** — run `npm install` inside `infra/`.
- **Docker daemon not running** — Lambda bundling needs Docker. Start
  Docker Desktop or set `CDK_DOCKER` to a podman wrapper.
- **`Resource handler returned message: "Reserved concurrency cannot be
  greater than the account's available concurrency..."`** — your account
  has too low an unreserved pool. Lower the values in `lambdas.ts` or
  raise the account limit.
- **`pillowLayerArn` not available in region** — Klayers publishes
  separately per region; cross-region ARNs are not valid.
