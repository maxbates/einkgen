# einkgen — guide for AI coding agents

You're an AI assistant working in the einkgen repository. This file is your
fast-path orientation; everything else is one hop away.

## What this is

A small AWS pipeline that generates dithered images for an **Inkplate 10**
e-paper display. CLI / cron / future input channels write into an S3-prefix
queue → a generator Lambda drains it → S3 + CloudFront serve the manifest +
BMP to the device. The web app is a public read-only dashboard with three
tabs (Queue, History, Device).

Live example: <https://d3r4vmga971o51.cloudfront.net/>

For full system shape: [ARCHITECTURE.md](ARCHITECTURE.md).
For deploy walkthrough: [QUICKSTART.md](QUICKSTART.md).
For decisions / open questions: [PLAN.md](PLAN.md).

---

## If the user just arrived (likely intent: deploy)

The most common first-time interaction is *"I want to deploy this."* Do this:

1. **Confirm Part 1 prerequisites are done.** [QUICKSTART.md Part 1](QUICKSTART.md#part-1--human-prerequisites-do-these-first)
   is humans-only — accounts, local tooling, AWS profile, the device-status
   token. If any step is unclear, walk them through it; don't try to do
   account creation or local installs on their behalf.
2. **Collect the four inputs you need to run Part 3:**
   - AWS profile name (default `einkgen`)
   - AWS region (default `us-east-1`)
   - Environment name (default `dev`)
   - OpenAI API key + device-status token — ask the user to paste these
     when you reach [§3.5](QUICKSTART.md#35-populate-the-secrets), not
     up-front. Never echo either value back at them.
3. **Execute Part 3 step by step.** Summarise each command's output;
   stop and ask if anything looks unexpected. Don't paper over failures.
4. **After §3.7,** open the deployed CloudFront URL and confirm the three
   tabs render. The `Queue` tab should say "Queue is empty.", `Device`
   should say "Device has not reported yet.", `History` will fill in on
   the first cron tick (within 2 h) or on the first manual enqueue (§3.9).

For anything else, jump to the table below.

---

## File map

```
README.md                       slim overview + doc index (start here for humans)
CLAUDE.md                       ← you are here
ARCHITECTURE.md                 system design (§1 device → §12 threat model)
PLAN.md                         plan, decisions, open questions
QUICKSTART.md                   deploy walkthrough (Part 1 human / Part 3 agent)
TODOS.md                        open follow-ups, by priority
CHANGELOG.md                    release history
VERSION                         4-digit version (MAJOR.MINOR.PATCH.MICRO)
pyproject.toml                  Python package + dev deps + `einkgen` entry point
.env.example                    CLI env vars

src/einkgen/
├── __main__.py                 `python -m einkgen`
├── cli/                        user-facing CLI commands
│   ├── __init__.py             top-level dispatcher
│   ├── status.py               einkgen status
│   ├── history.py              einkgen history
│   ├── queue.py                einkgen queue {ls,rm,prompt,image}
│   └── local.py                einkgen local {generate,convert,preview}
├── core/                       shared image/queue/publish logic (CLI ↔ Lambda)
│   ├── generate.py             OpenAI gpt-image-1 + BASE_PROMPT + PROMPT_LIBRARY
│   ├── convert.py              crop + grayscale + Atkinson dither + 8-bit BMP
│   ├── publish.py              writes current/, archives history/, CF invalidate
│   ├── manifest.py             manifest schema + next_check_after
│   ├── queue.py                S3-prefix queue (enqueue/pop_head/list/cancel)
│   ├── pipeline.py             one queue item → published frame
│   └── s3.py                   thin boto3 wrapper
└── lambdas/
    ├── generator.py            S3 event + cron handlers
    ├── read_api.py             GET /queue, /history, /status
    └── device_status.py        POST / (X-Device-Token)

web/                            React + Vite SPA (read-only dashboard)
├── src/api.ts                  typed client for read-api Lambda
├── src/format.ts               pure helpers (timestamps, hashes) — has unit tests
└── src/tabs/{Queue,History,Device}.tsx

firmware/inkplate10/            Arduino sketch + own README
├── inkplate10.ino              main sketch
├── README.md                   build & flash instructions
└── secrets.h.example           (real secrets.h is gitignored)

infra/                          AWS CDK stack (TypeScript)
├── bin/einkgen.ts              CDK app entry (one stack per env)
├── lib/einkgen-stack.ts        top-level wiring
├── lib/lambdas.ts              3 Lambdas + 2 API Gateway HTTP APIs + EventBridge
├── lib/bucket.ts               S3 bucket (public access blocked, OAC for CDN)
├── lib/cloudfront.ts           distribution + viewer-request gate on history/*
├── lib/secrets.ts              openai_api_key + device_status_token
├── lib/observability.ts        log retention + ERROR metric filters + dashboard
├── lambda/                     per-Lambda requirements.txt + staged Python src
├── scripts/check-errors.sh     24h ERROR sweep across all 3 Lambdas
└── README.md                   CDK-internal reference

tests/                          pytest, moto-backed (boto3 is stubbed)
```

---

## Common requests

| User says... | Do this |
| --- | --- |
| "Deploy this" / "Set this up" / "Get it running" | [QUICKSTART.md](QUICKSTART.md), follow Part 3 |
| "What is this?" / "How does X work?" | [ARCHITECTURE.md](ARCHITECTURE.md) §1–§12 — pick the matching section |
| "Add a CLI subcommand" | `src/einkgen/cli/<name>.py` + register in `cli/__init__.py` |
| "Add a route on the read-api" | `src/einkgen/lambdas/read_api.py` + a test; CORS is pinned at API Gateway in `infra/lib/lambdas.ts` |
| "Change the dither algorithm" | `src/einkgen/core/convert.py`. **Read [TODOS.md](TODOS.md) §"Profile and replace pure-Python error-diffusion dither" first** — the current pure-Python Atkinson is the considered choice. Don't replace without re-measuring. |
| "It's broken / debug this" | `AWS_PROFILE=einkgen ./infra/scripts/check-errors.sh 24h` first, then `aws logs tail /aws/lambda/<fn> --follow` |
| "QA the live SPA" | Use the deployed CloudFront URL and the browse tool (or `/qa-only` if gstack is loaded) |
| "Cut a release" | Bump `VERSION`, prepend a `CHANGELOG.md` entry, then redeploy as in [QUICKSTART §3.6–§3.7](QUICKSTART.md#36-build-the-web-spa-against-the-deployed-urls) |
| "Tear it all down" | `( cd infra && AWS_PROFILE=… npx cdk destroy -c env=<env> )` — **always confirm with the user first** |

---

## Hard rules

- **OpenAI cost.** Each generator invocation costs ~$0.04 (gpt-image-1 at
  1536×1024). Don't enqueue more than 1–2 test prompts per session.
  Don't trigger cron faster than its 2 h rate. Don't "fix" things by
  running the generator in a loop. There is **no daily $ cap yet** (see
  [TODOS.md](TODOS.md)).
- **Secrets.** Never echo the OpenAI key or device-status token back to
  the user. When verifying, print *length only* (`wc -c`) or a SHA-256
  prefix. Never commit `.env`, `firmware/inkplate10/secrets.h`, or
  `infra/cdk-outputs.json` — all three are gitignored for a reason.
- **Destructive AWS ops require explicit human confirmation** in the
  current message, every time:
  - `cdk destroy`, `aws s3 rm --recursive`, `aws secretsmanager delete-secret`
  - `cdk deploy` to any environment that looks like prod (name contains
    `prod`, `production`, `live`)
  - force-pushing or rewriting history on `main`
  `cdk deploy` to a dev env may be standing-approved per-maintainer (see
  below); confirm scope before running it for a new user.
- **Don't touch `firmware/inkplate10/secrets.h`.** It's gitignored and
  filled in by the human when the physical device arrives. Don't
  generate it.
- **`config.toml`, `.env.example`, `.gitignore`** — read-mostly. Edit
  only when adding a new key or fixing a real bug.

---

## Conventions

- **Doc cross-refs.** Code comments use `ARCHITECTURE §N` (1–12) or
  `PLAN §N` (1–4). The old `README §N` style is historical — don't
  reintroduce it.
- **No new top-level docs without a reason.** The split is README +
  ARCHITECTURE + PLAN + QUICKSTART + TODOS + CHANGELOG + CLAUDE. New
  material belongs as a section in an existing doc unless it's a new
  operational mode (e.g. a future `RUNBOOK.md` for incident response).
- **Tests use moto.** Don't add tests that hit real AWS. The pytest
  fixtures in `tests/conftest.py` stub the boto3 layer; follow the
  existing pattern.
- **Versioning.** 4-digit `MAJOR.MINOR.PATCH.MICRO` in `VERSION`. Every
  user-visible change goes in `CHANGELOG.md` under `[Added] / [Changed]
  / [Fixed] / [Security]`.
- **Web SPA.** No UI library, no router, no state library. Vanilla CSS
  in `web/src/`. Keep it small.
- **Lambda runtime.** Python 3.12, arm64 (Graviton2). Pillow is bundled
  per-function via the CDK asset bundler; don't add a Pillow Lambda
  layer.

---

## Standing approvals (per-maintainer)

This section is project-specific to the original maintainer's local
environment and lives in their `~/.claude/.../memory/` as well — don't
generalise it for a new user.

- **Max Bates** (`maxbates@gmail.com`) has standing approval for
  `cdk deploy` to AWS account **619848530148** (`einkgen` profile,
  `us-east-1`). No per-invocation confirmation needed for that account.

For any other user / account, always confirm before the first
`cdk deploy`. After that, ask the user if they want to grant standing
approval for the session.
