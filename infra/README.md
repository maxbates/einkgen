# einkgen infra (AWS CDK, TypeScript)

Single CDK stack provisioning everything in [ARCHITECTURE §9](../ARCHITECTURE.md#9-aws-infrastructure):
one S3 bucket, one CloudFront distribution, three Lambdas (generator,
read-api, device-status) — the latter two fronted by API Gateway HTTP APIs —
two Secrets Manager secrets, an EventBridge cron rule, log-group retention,
a CloudWatch dashboard, and an ERROR metric filter per Lambda.

For a step-by-step deploy walkthrough see [QUICKSTART.md](../QUICKSTART.md).
This file is the reference for how the CDK code is organised.

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
│   ├── lambdas.ts             3 Lambdas + triggers + 2 API Gateway HTTP APIs
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

End-to-end deploy is documented in [QUICKSTART.md](../QUICKSTART.md). The
common CDK commands once you're in `infra/`:

```sh
npm install
npx cdk synth                              # synth without web (default)
npx cdk synth   -c includeWebAssets=true   # full synth (requires web/dist/)
npx cdk deploy  -c env=dev
npx cdk diff    -c env=dev
npx cdk destroy -c env=dev
```

The `env` context value drives the bucket name (`einkgen-<env>`) and the
stack id (`EinkgenStack-<env>`). Default `dev`. `includeWebAssets` defaults
to `false`; set to `true` for the second deploy after `web/dist/` exists.

Secrets (`einkgen/openai_api_key`, `einkgen/device_status_token`) are
created as placeholder values by the stack. Populate them post-deploy
with `aws secretsmanager put-secret-value` — see [QUICKSTART.md §3.5](../QUICKSTART.md#35-populate-the-secrets).

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
needs (read-api and device-status don't carry Pillow/openai). Pillow is
bundled this way directly into the generator zip — see CHANGELOG.md
[0.2.0.1] for why we don't use a published Pillow layer.

All three Lambdas run on **arm64 (Graviton2)**. Bundles use the
`public.ecr.aws/sam/build-python3.12-arm64` image so wheels resolve for
the right architecture.

When `src/einkgen/` is missing (as in the parallel-track worktrees) the
stager writes a stub `einkgen/__init__.py` so `cdk synth` still succeeds —
intended only for synth smoke-tests. Real deploys must run from a tree
where `src/einkgen/` exists.

## API Gateway / CORS notes

Both `einkgen-read-api` and `einkgen-device-status` are fronted by API
Gateway HTTP APIs (not Lambda Function URLs — AWS's account-level "block
public access for Function URLs" rejects `AuthType: NONE`; see CHANGELOG.md
[0.2.0.1]). The web app talks to `ReadApiUrl` directly; the firmware POSTs
to `DeviceStatusUrl`.

- `einkgen-read-api` CORS is pinned at the API Gateway level to the
  CloudFront distribution domain plus `http://localhost:5173` for dev.
- `einkgen-device-status` has **no API-Gateway CORS configuration** —
  firmware-only, no browser is in the loop. (The Lambda's response
  hardcodes `Access-Control-Allow-Origin: *`; see
  [PLAN.md §4](../PLAN.md#4-open-questions) — pending cleanup.)

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
