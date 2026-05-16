# einkgen — guide for AI coding agents

You're an AI assistant working in the einkgen repository. This file is your
fast-path orientation; everything else is one hop away.

## What this is

A small AWS pipeline that generates dithered images for an **Inkplate 10**
e-paper display. CLI / cron / inbound-email / admin-tab write into an
S3-prefix queue → a generator Lambda drains it → S3 + CloudFront serve the
manifest + BMP to the device. The web app is a public dashboard with three
read-only tabs (Queue, History, Device) plus a password-gated **Admin** tab
for submitting prompts/images from a phone or laptop.

Live example: <https://einkgen.link/>

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
   - OpenAI API key + device-status token + admin password — ask the user
     to paste these when you reach
     [§3.5](QUICKSTART.md#35-populate-the-secrets), not up-front. Never
     echo any of them back at them.
3. **Execute Part 3 step by step.** Summarise each command's output;
   stop and ask if anything looks unexpected. Don't paper over failures.
4. **After §3.7,** open the deployed CloudFront URL and confirm all four
   tabs render. `Queue` should say "Queue is empty.", `Device` should say
   "Device has not reported yet.", `History` will fill in on the first
   cron tick (within 2 h) or on the first manual enqueue (§3.9), and the
   `Admin` tab should show a password prompt. Confirm the password from
   §3.5 logs in and stays in (90-day cookie).

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
│   ├── queue.py                einkgen queue {ls,rm,prompt,image} (image takes --prompt for restyle)
│   ├── allowlist.py            einkgen allowlist {ls,add,rm} (inbound-email senders)
│   └── local.py                einkgen local {generate,convert,preview}
├── core/                       shared image/queue/publish logic (CLI ↔ Lambda)
│   ├── generate.py             OpenAI gpt-image-2 generate + edit + BASE_PROMPT + PROMPT_LIBRARY (quality=medium)
│   ├── convert.py              crop + grayscale + Atkinson dither + 8-bit BMP
│   ├── publish.py              writes current/, archives history/, CF invalidate
│   ├── manifest.py             manifest schema + next_check_after
│   ├── queue.py                S3-prefix queue (enqueue/pop_head/list/cancel)
│   ├── pipeline.py             one queue item → published frame
│   ├── email_allowlist.py      S3-backed sender allowlist for inbound email
│   ├── email_parse.py          MIME parse + SPF/DKIM check from SES auth headers
│   ├── admin_cookie.py         HMAC-signed session cookie for the SPA Admin tab
│   └── s3.py                   thin boto3 wrapper
└── lambdas/
    ├── generator.py            S3 event + cron handlers
    ├── read_api.py             GET /queue, /history, /status
    ├── device_status.py        POST / (X-Device-Token)
    ├── inbound_email.py        S3-triggered SES inbound parser → queue.enqueue
    └── admin_api.py            POST /admin/{login,logout,queue/prompt,queue/image} + GET /admin/me

web/                            React + Vite SPA (read-only dashboard + admin tab)
├── src/api.ts                  typed client for read-api + admin-api Lambdas
├── src/format.ts               pure helpers (timestamps, hashes) — has unit tests
└── src/tabs/{Queue,History,Device,Admin}.tsx

firmware/inkplate10/            Arduino sketch + own README
├── inkplate10.ino              main sketch
├── README.md                   build & flash instructions
└── secrets.h.example           (real secrets.h is gitignored)

shortcuts/                      iPhone / Siri integration (docs only)
└── README.md                   email + HTTP shortcut walkthroughs for the iOS Shortcuts app

infra/                          AWS CDK stack (TypeScript)
├── bin/einkgen.ts              CDK app entry (one stack per env)
├── lib/einkgen-stack.ts        top-level wiring
├── lib/lambdas.ts              4 base Lambdas + 3 API Gateway HTTP APIs + EventBridge
├── lib/inbound-email.ts        opt-in SES inbound stack (gated by einkgenInboundDomain context)
├── lib/bucket.ts               S3 bucket (public access blocked, OAC for CDN)
├── lib/cloudfront.ts           distribution + viewer-request gate on history/* (admin/* behavior added in einkgen-stack.ts)
├── lib/secrets.ts              openai_api_key + device_status_token + admin_password + admin_cookie_signing_key
├── lib/observability.ts        log retention + ERROR metric filters + dashboard
├── lambda/                     per-Lambda requirements.txt + staged Python src
├── scripts/check-errors.sh     24h ERROR sweep across all Lambdas
├── scripts/register-domain.example.sh  Route 53 domain registration template (copy to .sh, fill PII)
└── README.md                   CDK-internal reference

tests/                          pytest, moto-backed (boto3 is stubbed)
```

---

## Common requests

| User says... | Do this |
| --- | --- |
| "Deploy this" / "Set this up" / "Get it running" | [QUICKSTART.md](QUICKSTART.md), follow Part 3 |
| "Redeploy" / "Push the latest code" / "Roll out my change" | `( cd infra && AWS_PROFILE=einkgen npx cdk deploy --require-approval never )`. The canonical site + inbound domain (`einkgen.link`) live in [infra/cdk.json](infra/cdk.json) `context` so a **bare** `cdk deploy` keeps all live wiring intact. Add `-c includeWebAssets=true` only when you just rebuilt `web/dist` and want CloudFront to pick it up. **Do not** override `einkgenSiteDomain` / `einkgenInboundDomain` to empty unless you actually want to tear down the custom domain or inbound email — see Hard rule on context-stripping below. |
| "What is this?" / "How does X work?" | [ARCHITECTURE.md](ARCHITECTURE.md) §1–§12 — pick the matching section |
| "Add a CLI subcommand" | `src/einkgen/cli/<name>.py` + register in `cli/__init__.py` |
| "Add a route on the read-api" | `src/einkgen/lambdas/read_api.py` + a test; CORS is pinned at API Gateway in `infra/lib/lambdas.ts` |
| "Add a route on the admin-api" / "Add a thing the operator can do from the SPA" | `src/einkgen/lambdas/admin_api.py` (cookie-gated dispatcher) + a test in `tests/test_lambda_admin_api.py` + a typed client in `web/src/api.ts` + UI in `web/src/tabs/Admin.tsx`. No CORS — admin endpoints are same-origin via the CloudFront `/admin/*` behavior wired in `infra/lib/einkgen-stack.ts`. |
| "Rotate the admin password" | `aws secretsmanager put-secret-value --secret-id einkgen/admin_password --secret-string '<new>'` (takes effect ≤5 min on warm Lambdas). To log every existing browser session out as well, rotate `einkgen/admin_cookie_signing_key` the same way. |
| "Run the tests" / "Run the test suite" | `uv run --extra dev pytest` from the repo (or worktree) root. `uv` syncs `.venv/` from `pyproject.toml` on demand and reuses a global wheel cache (`~/.cache/uv/`), so first run in a fresh worktree is one-time-slow and every subsequent run is seconds. Do **not** bootstrap with bare `pip install -e ".[dev]"` + `pytest` — pip has no shared cache and the system Python on macOS dev boxes often doesn't satisfy `requires-python >=3.11`, so it re-downloads everything every time and may pick the wrong interpreter. |
| "Set up email submission" / "Enable inbound email" | Already on for the canonical `einkgen.link` deploy via [infra/cdk.json](infra/cdk.json) context. For a **new** domain, follow [QUICKSTART §3.10](QUICKSTART.md#310-optional-email-submission-channel) — pick path A (register a new domain via `infra/scripts/register-domain.sh`) or B (delegate an existing domain to Route 53), edit `einkgenInboundDomain` in `infra/cdk.json` to that domain (or pass `-c einkgenInboundDomain=<domain>` to override), redeploy. DKIM CNAMEs + MX are auto-created by CDK; **receipt-rule activation is one-time-manual** (`aws ses set-active-receipt-rule-set --rule-set-name einkgen-inbound`) — survives all future redeploys. |
| "Add an allowed email sender" | `einkgen allowlist add <email>` (writes `config/email_allowlist.txt`). Comparison is case-insensitive. Never hardcode addresses in committed CDK — first-deploy seeding goes through the `einkgenAllowlistSeed` context flag instead. |
| "Pick me a cheap domain" | `aws route53domains list-prices --region us-east-1` + filter where reg ≤ $10 *and* renew ≤ $10. Then `check-domain-availability` per candidate. Always surface renewal price — domain registration is a recurring cost. Don't autonomously register; have the human `cp register-domain.example.sh register-domain.sh` and fill in their PII (the live `.sh` is gitignored). |
| "Change the dither algorithm" | `src/einkgen/core/convert.py`. **Read [TODOS.md](TODOS.md) §"Profile and replace pure-Python error-diffusion dither" first** — the current pure-Python Atkinson is the considered choice. Don't replace without re-measuring. |
| "Change the device poll interval" / "make it check more often" | Edit **both** `SLEEP_MAX_SECONDS` + `SLEEP_FALLBACK_SECONDS` in [firmware/inkplate10/inkplate10.ino](firmware/inkplate10/inkplate10.ino) **and** redeploy with `-c einkgenPollIntervalSeconds=<n>`. See [QUICKSTART §3.12](QUICKSTART.md#312-optional-device-poll-interval) for the battery-life table. Server-only change is silently clamped by firmware. Don't conflate with the auto-gen `rate(2 hours)` cron — that's the OpenAI-cost knob, separate concern. |
| "It's broken / debug this" | `AWS_PROFILE=einkgen ./infra/scripts/check-errors.sh 24h` first, then `aws logs tail /aws/lambda/<fn> --follow` |
| "QA the live SPA" | Use the deployed CloudFront URL and the browse tool (or `/qa-only` if gstack is loaded) |
| "Set up an iPhone shortcut" / "Submit from Siri" / "Phone shortcut" | [shortcuts/README.md](shortcuts/README.md) — two paths: a 2-action email shortcut (if inbound email is set up) or a 4–8-action HTTP shortcut that calls the admin API. Both end with *"Hey Siri, einkgen."* |
| "Cut a release" | Bump `VERSION`, prepend a `CHANGELOG.md` entry, then redeploy as in [QUICKSTART §3.6–§3.7](QUICKSTART.md#36-build-the-web-spa-against-the-deployed-urls) |
| "Tear it all down" | `( cd infra && AWS_PROFILE=… npx cdk destroy -c env=<env> )` — **always confirm with the user first** |

---

## Hard rules

- **Don't strip `cdk.json` context on deploy.** `einkgenSiteDomain` and
  `einkgenInboundDomain` live in [infra/cdk.json](infra/cdk.json)
  `context` so a bare `cdk deploy` preserves the live wiring. Passing
  `-c einkgenSiteDomain=` (empty string), `-c einkgenInboundDomain=`,
  or removing those keys from `cdk.json` tells CDK to **delete**: the
  ACM cert, both CloudFront aliases, the apex A + AAAA Route 53
  records, the MX record, all three DKIM CNAMEs, the inbound-email
  Lambda, the SES domain identity, and the SES receipt rule set. The
  site stops resolving (no A record) and inbound email stops being
  received. This has happened **twice** — both times because someone
  redeployed without remembering the override flags, before they were
  baked into `cdk.json`. The fix is now permanent at the file level;
  don't undo it. To intentionally tear those resources down (e.g.
  retiring the domain), do it as a deliberate two-step: edit
  `cdk.json` to remove the keys, commit the change with a clear
  message, then deploy. Always run `cdk diff` first and **always**
  confirm with the human before deploying a diff that deletes any
  `CdnSite*`, `InboundEmail*`, or `*Route53*` resource.
- **OpenAI cost.** Each generator invocation calls `gpt-image-2` at
  1536×1024 with `quality="medium"` — cheaper than the previous
  `gpt-image-1` high-quality default, but still real per-call $. Don't
  enqueue more than 1–2 test prompts per session. Don't trigger cron
  faster than its 2 h rate. Don't "fix" things by running the generator
  in a loop. There is **no daily $ cap yet** (see [TODOS.md](TODOS.md)).
- **Domain registration is a recurring cost.** Never auto-register a
  domain via `route53domains register-domain`. Always present the
  human with the renewal price and let them approve / pick the name
  before they run their copy of `register-domain.sh`. ICANN-required
  contact info goes in the script — never make it up.
- **Secrets.** Never echo the OpenAI key, device-status token, **admin
  password**, or admin cookie-signing key back to the user. When
  verifying, print *length only* (`wc -c`) or a SHA-256 prefix. Never
  commit `.env`, `firmware/inkplate10/secrets.h`, or
  `infra/cdk-outputs.json` — all three are gitignored for a reason.
- **PII / allowlist data stays out of git.** Don't hardcode email
  addresses in committed CDK or Python — including in
  `seedAllowlist`. First-deploy seeding goes through the
  `einkgenAllowlistSeed` CDK context flag; the durable list lives in
  `s3://<bucket>/config/email_allowlist.txt` and is edited via
  `einkgen allowlist {add,rm}`. The S3 file is never committed.
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
  existing pattern. Run them with `uv run --extra dev pytest` — `uv`
  auto-syncs `.venv/` from `pyproject.toml` and reuses a global wheel
  cache, so fresh worktrees install once and run in seconds thereafter.
  Don't reach for system Python directly; `requires-python >=3.11` is
  not guaranteed there and `moto` won't be installed.
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
