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
│   ├── queue.py                einkgen queue {ls,rm,prompt,image} (image takes --prompt for restyle; prompt/image take --top or --now to skip the buffer)
│   ├── allowlist.py            einkgen allowlist {ls,add,rm} (inbound-email senders)
│   ├── prompts.py              einkgen prompts {ls,edit,reset} (random-pick library)
│   └── local.py                einkgen local {generate,convert,preview}
├── core/                       shared image/queue/publish logic (CLI ↔ Lambda)
│   ├── generate.py             OpenAI gpt-image-2 generate + edit + BASE_PROMPT (quality=medium); random_prompt() → prompt_library
│   ├── prompt_library.py       S3-backed *topic* bank (`config/prompt_library.txt`); operator-editable via Admin tab + CLI. Cron picks topics + expands via expand_topic() before enqueueing
│   ├── convert.py              crop + grayscale + Atkinson dither + 8-bit BMP
│   ├── publish.py              `archive_to_history` (history-only, used by cron buffer path) + `publish` (history + current, used by admin Now/Run) + `set_current_from_history` (re-point manifest at past frame; used by /wake, /admin/show)
│   ├── manifest.py             manifest schema + next_check_after
│   ├── queue.py                Prompt queue. S3-prefix two-priority buffer; key format `queue/<priority>-<iso_ts>-<ulid>.json` where priority is "0" (top) or "1" (bottom). No in-place mutation. enqueue(..., at="top|bottom") / peek_head / get / cancel / count. No move_to_top — see render_item action on the generator instead.
│   ├── generated_queue.py      Generated queue (NEW in 0.6.0.0). FIFO buffer of pre-rendered frames between the prompt queue and history. Each marker at `generated/<iso_ts>-<history_id>.json` points at an existing `history/<id>/` archive. enqueue / peek_head / get / finalize / cancel / count. /wake pops the head; admin skip/show drops the marker.
│   ├── pipeline.py             one queue item → rendered frame. Two flows: `buffer_item` (cron + render_one — archives to history/ + enqueues a generated/ marker, no current/ touch) and `publish_item` (admin Now/Run — archives + sets current). Shared front half in `_render`.
│   ├── email_allowlist.py      S3-backed sender allowlist for inbound email
│   ├── email_parse.py          MIME parse + SPF/DKIM check from SES auth headers
│   ├── admin_cookie.py         HMAC-signed session cookie for the SPA Admin tab
│   └── s3.py                   thin boto3 wrapper
└── lambdas/
    ├── generator.py            cron (refill the generated buffer to TARGET_GENERATED_QUEUE_LENGTH; tops the prompt queue up inline via expand_topic as it drains; trailing top-up leaves prompt queue at floor for SPA viewing) + direct-invoke `{"action":"render_now"}` (Admin Now — sets current, bypasses buffer) + `{"action":"render_item","item_id":...}` (per-row Run — sets current, bypasses buffer) + `{"action":"render_one"}` (/wake replenish — buffer only). Lambda timeout = 15 min (Lambda max) to fit cold-start refills. NO S3 ObjectCreated trigger since [0.5.0.0]. Cron does NOT touch current/ since [0.6.0.0].
    ├── read_api.py             GET /queue, /generated, /history, /status
    ├── device_status.py        POST / (status heartbeat) + POST /wake (sha-debounced display advance: pops head of generated queue, sets current, fires render_one to replenish). Both X-Device-Token.
    ├── inbound_email.py        S3-triggered SES inbound parser → queue.enqueue. Subject prefix `NOW `, `NOW:`, or `[NOW]` (case-insensitive) enqueues at top + async-invokes generator render_now; trigger is stripped from the prompt.
    └── admin_api.py            POST /admin/{login,logout,queue/prompt,queue/image,prompts/reset,show} + POST /admin/queue/<id>/run + DELETE /admin/queue/<id> + DELETE /admin/generated/<history_id> (skip a buffered render) + GET/PUT /admin/prompts + GET /admin/me. Enqueue accepts at="top|bottom|now". "now" async-invokes generator with render_now (renders head, sets current); /run async-invokes with render_item (renders that specific id, sets current, no reorder). /admin/show also drops the matching generated-queue marker.

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
├── scripts/deploy.sh           **canonical redeploy** — rebuild SPA against live URLs, cdk deploy, verify
├── scripts/verify-deploy.sh    **post-deploy smoke test** — curl-only end-to-end checks, exits non-zero on any fail
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
| "Redeploy" / "Push the latest code" / "Roll out my change" | **Prefer `AWS_PROFILE=einkgen ./infra/scripts/deploy.sh`** — it pulls the live API URLs from CloudFormation, rebuilds the SPA against them, fails fast if the bundle still has `localhost:` in it, runs `cdk deploy` (no overrides), and then runs `verify-deploy.sh`. Use `--no-web` for infra-only redeploys. The bare `( cd infra && AWS_PROFILE=einkgen npx cdk deploy --require-approval never )` still works for infra-only iteration, but does NOT rebuild the SPA — if you forget the rebuild step on a fresh worktree (no `web/.env.production`), the deployed bundle bakes in `localhost:3001` and every tab in the dashboard says "Loading…" forever. The canonical site + inbound domain (`einkgen.link`) live in [infra/cdk.json](infra/cdk.json) `context`. **Do not** override `einkgenSiteDomain` / `einkgenInboundDomain` to empty unless you actually want to tear down the custom domain or inbound email — see Hard rule on context-stripping below. |
| "Verify the deploy" / "Smoke test" / "Did it actually ship" | `AWS_PROFILE=einkgen ./infra/scripts/verify-deploy.sh` — reads live stack outputs from CFN, then exercises read-api, admin-api (direct + via CloudFront), `/current/manifest.json` + `/current/image.bmp`, the SPA shell, and the SPA bundle (no `localhost:`, refs the real read-api host, refs the CDN host), plus a 30-min ERROR-log sweep across all four Lambdas. Exits non-zero on any fail. Run after every deploy — `deploy.sh` chains it automatically. |
| "What is this?" / "How does X work?" | [ARCHITECTURE.md](ARCHITECTURE.md) §1–§12 — pick the matching section |
| "Add a CLI subcommand" | `src/einkgen/cli/<name>.py` + register in `cli/__init__.py` |
| "Add a route on the read-api" | `src/einkgen/lambdas/read_api.py` + a test; CORS is pinned at API Gateway in `infra/lib/lambdas.ts` |
| "Add a route on the admin-api" / "Add a thing the operator can do from the SPA" | `src/einkgen/lambdas/admin_api.py` (cookie-gated dispatcher) + a test in `tests/test_lambda_admin_api.py` + a typed client in `web/src/api.ts` + UI in `web/src/tabs/Admin.tsx` (or `History.tsx` for `/admin/show`). No CORS — admin endpoints are same-origin via the CloudFront `/admin/*` behavior wired in `infra/lib/einkgen-stack.ts`. |
| "Show an old image again" / "Pin a history frame" / "Re-display past frame" | `POST /admin/show` with `{"history_id": "..."}`. Rewrites `current/manifest.json` to point at `history/<id>/processed.bmp` (no byte copy, no OpenAI call). The History tab's details modal exposes this as a **Show this now** button for logged-in operators; a "Now showing" eye badge marks whichever tile is currently being drawn. See [`set_current_from_history`](src/einkgen/core/publish.py). |
| "Rotate the admin password" | `aws secretsmanager put-secret-value --secret-id einkgen/admin_password --secret-string '<new>'` (takes effect ≤5 min on warm Lambdas). To log every existing browser session out as well, rotate `einkgen/admin_cookie_signing_key` the same way. |
| "Run the tests" / "Run the test suite" | `uv run --extra dev pytest` from the repo (or worktree) root. `uv` syncs `.venv/` from `pyproject.toml` on demand and reuses a global wheel cache (`~/.cache/uv/`), so first run in a fresh worktree is one-time-slow and every subsequent run is seconds. Do **not** bootstrap with bare `pip install -e ".[dev]"` + `pytest` — pip has no shared cache and the system Python on macOS dev boxes often doesn't satisfy `requires-python >=3.11`, so it re-downloads everything every time and may pick the wrong interpreter. |
| "Set up email submission" / "Enable inbound email" | Already on for the canonical `einkgen.link` deploy via [infra/cdk.json](infra/cdk.json) context. For a **new** domain, follow [QUICKSTART §3.10](QUICKSTART.md#310-optional-email-submission-channel) — pick path A (register a new domain via `infra/scripts/register-domain.sh`) or B (delegate an existing domain to Route 53), edit `einkgenInboundDomain` in `infra/cdk.json` to that domain (or pass `-c einkgenInboundDomain=<domain>` to override), redeploy. DKIM CNAMEs + MX are auto-created by CDK; **receipt-rule activation is one-time-manual** (`aws ses set-active-receipt-rule-set --rule-set-name einkgen-inbound`) — survives all future redeploys. |
| "Add an allowed email sender" | `einkgen allowlist add <email>` (writes `config/email_allowlist.txt`). Comparison is case-insensitive. Never hardcode addresses in committed CDK — first-deploy seeding goes through the `einkgenAllowlistSeed` context flag instead. |
| "Edit the random prompt bank" / "Change what the cron picks from" / "Add/remove a topic" | Edit from the SPA **Admin** tab (textarea, one topic per line, Save / Reset to defaults) or via `einkgen prompts {ls,edit,reset}`. Persists to `s3://<bucket>/config/prompt_library.txt`; Lambda picks up changes within ~60 s (warm-container cache TTL). Missing/empty file → falls back to the 10 seed defaults baked into `core/prompt_library.py::DEFAULTS`. Each line is a *topic*, not a finished prompt: the cron picks one and runs it through `generate.expand_topic()` (text LLM, default `gpt-5-mini`) before enqueueing the expansion as `kind="prompt"`. |
| "Run this queue item now" / "Delete a pending item" | Use the SPA **Queue** tab while logged in as admin. Per-row buttons: **Run** (render this specific prompt next, regardless of queue order — calls `POST /admin/queue/<id>/run` which async-invokes the generator with `render_item`; bypasses the generated buffer and sets current directly) and **Remove** (cancel — `DELETE /admin/queue/<id>`). There is **no per-row move-to-top** — the queue is two-priority (top / bottom) and items aren't reordered after enqueue. Pick the right placement at submit time (Top / Bottom / Now buttons on the Admin form), or use Run to bypass order for a specific item. |
| "Submit and render now" / "Skip the buffer" / "I want this on the panel right away" | Three paths, all do the same thing (enqueue at top + async-invoke generator with `render_now`): (a) SPA Admin tab — the **Now** button on the prompt/image form; (b) CLI — `einkgen queue {prompt,image} … --now` (since 0.6.2.0); (c) inbound email — subject prefix `NOW `, `NOW:`, or `[NOW]` (case-insensitive; the trigger is stripped before generation). Without one of these, submissions land behind the 10-deep pre-rendered buffer and surface ~5h later. The CLI's `--now` needs `lambda:InvokeFunction` on `einkgen-generator` in the operator's IAM; the email Lambda gets it via `generator.grantInvoke` in [infra/lib/inbound-email.ts](infra/lib/inbound-email.ts). |
| "Skip a pre-rendered image" / "Drop something from the buffer" / "Show this buffered image now" | Use the SPA **Queue** tab → **Up next on the device** section while logged in as admin. Per-row buttons: **Skip** (drop the marker so the device never auto-advances to it — `DELETE /admin/generated/<history_id>`; the bytes stay in History so it can still be pinned later) and **Show now** (set as current immediately, jumping the buffer order — calls `POST /admin/show` with `history_id`, which also drops the marker). The buffer is FIFO with target depth 10; cron tops it up to that floor each tick, capped at `MAX_RENDERS_PER_TICK = 2` renders per cron tick. |
| "Why is `current/image.bmp` stale?" / "What writes to current/?" | Since [0.6.0.0] cron does NOT touch `current/`. The legacy `publish()` path (`current/manifest.json` pointing at `current/image.bmp`) is still used by admin **Now** / **Run** for the operator-immediate cases. Otherwise, `current/manifest.json` points at `history/<id>/processed.bmp` and is rewritten by `set_current_from_history` (the `/wake` advance + `/admin/show`). `current/image.bmp` itself is only written by the legacy paths and may lag behind what the device is actually drawing — the device reads `manifest.image_url`, which is always authoritative. |
| "Change the text-expansion model" / "Use a different model for the prompt expansion" | Set the `EINKGEN_TEXT_MODEL` env var on the generator Lambda (default `gpt-5-mini`). Cheaper/older options: `gpt-4o-mini`. Don't put image-only model names here — `expand_topic` calls `chat.completions.create`. |
| "Change how many items the queue keeps" / "Queue too short / too long" | Two knobs in [src/einkgen/lambdas/generator.py](src/einkgen/lambdas/generator.py): `TARGET_PROMPT_QUEUE_LENGTH` (= 5, floor for the prompt-queue text-LLM top-up; visible on the SPA between cron ticks) and `TARGET_GENERATED_QUEUE_LENGTH` (= 10, target depth of the pre-rendered buffer the device draws from; cron refills to this in a single tick). `MAX_RENDERS_PER_TICK` exists as a defensive safety bound; it's set above `TARGET_GENERATED_QUEUE_LENGTH` and shouldn't bite. Going up on the generated queue = more visibility into upcoming frames and bigger drain-resilience to rapid wake-button presses; costs the same per-frame OpenAI bill, just more renders bunched into the cron tick after a drain. The cron render cadence is the EventBridge `rate(30 minutes)` — separate knob (`einkgenPollIntervalSeconds`). |
| "Render faster / slower" / "Change cron cadence" / "Reduce OpenAI bill" | One knob: `einkgenPollIntervalSeconds` in [infra/cdk.json](infra/cdk.json) (default `"1800"` = 30 min). Drives BOTH the EventBridge cron rate AND the manifest's `next_check_after` hint. Edit + `AWS_PROFILE=einkgen ./infra/scripts/deploy.sh`. Values ≤3600 are server-only (firmware honours any sub-hour hint). Values >3600 also need `SLEEP_MAX_SECONDS` raised in [firmware/inkplate10/inkplate10.ino](firmware/inkplate10/inkplate10.ino) before re-flash. Must be a multiple of 60. Rough OpenAI cost: 15 min → ~$115/mo, 30 min → ~$55/mo, 1 h → ~$30/mo, 2 h → ~$15/mo. Battery scales inversely (30 min → ~3–4 months, 1 h → ~6–9 months on a 3 Ah cell). |
| "Pick me a cheap domain" | `aws route53domains list-prices --region us-east-1` + filter where reg ≤ $10 *and* renew ≤ $10. Then `check-domain-availability` per candidate. Always surface renewal price — domain registration is a recurring cost. Don't autonomously register; have the human `cp register-domain.example.sh register-domain.sh` and fill in their PII (the live `.sh` is gitignored). |
| "Change the dither algorithm" | `src/einkgen/core/convert.py`. **Read [TODOS.md](TODOS.md) §"Profile and replace pure-Python error-diffusion dither" first** — the current pure-Python Atkinson is the considered choice. Don't replace without re-measuring. |
| "Change the device poll interval" / "make it check more often" | Same knob as "Render faster / slower" above — `einkgenPollIntervalSeconds` drives both. There is no separate device-only knob since v0.5.1.0 (deliberately — no point polling more often than cron renders). |
| "It's broken / debug this" / "Site is down" / "Tabs won't load" | **Run `AWS_PROFILE=einkgen ./infra/scripts/verify-deploy.sh` first.** It pinpoints which of {read-api, admin-api, manifest, image, SPA shell, SPA bundle env-vars, recent Lambda errors} is broken. Then `./infra/scripts/check-errors.sh 24h` for deeper log context, and `aws logs tail /aws/lambda/<fn> --follow` for live tail. If verify reports an SPA bundle regression (no `localhost:` check, etc.), fix is **`AWS_PROFILE=einkgen ./infra/scripts/deploy.sh`** — do not "fix" by editing the SPA. |
| "QA the live SPA" | Use the deployed CloudFront URL and the browse tool (or `/qa-only` if gstack is loaded) |
| "Set up an iPhone shortcut" / "Submit from Siri" / "Phone shortcut" | [shortcuts/README.md](shortcuts/README.md) — two paths: a 2-action email shortcut (if inbound email is set up) or a 4–8-action HTTP shortcut that calls the admin API. Both end with *"Hey Siri, einkgen."* |
| "Cut a release" | Bump `VERSION`, prepend a `CHANGELOG.md` entry, commit, then `AWS_PROFILE=einkgen ./infra/scripts/deploy.sh` (the canonical redeploy path — see top of Hard rules below). |
| "Tear it all down" | `( cd infra && AWS_PROFILE=… npx cdk destroy -c env=<env> )` — **always confirm with the user first** |

---

## Hard rules

- **The canonical redeploy path is `AWS_PROFILE=einkgen ./infra/scripts/deploy.sh`.**
  Never `( cd web && npm run build )` followed by a bare `cdk deploy`
  on a fresh worktree — `web/.env.production` is gitignored, so without
  the wrapper's CFN-output fetch the Vite build silently falls back to
  `http://localhost:3001` and the deployed SPA's Queue / History /
  Device tabs spin forever. We shipped this regression to prod **twice**
  before the wrapper existed. The wrapper fails fast if the freshly-built
  bundle still contains `localhost:` and finishes by running
  `./infra/scripts/verify-deploy.sh`, which exits non-zero on any
  regression. After every deploy, the only acceptable end state is "14
  pass / 0 fail" (or higher pass count if checks are added). If you have
  a reason to skip the SPA rebuild step, use `deploy.sh --no-web`; don't
  open-code the bare `cdk deploy` from memory.

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
  1200×832 with `quality="medium"` — cheaper than the original 1536×1024
  `gpt-image-1` high-quality default by a wide margin (37 % fewer pixels
  on top of the medium-quality drop), but still real per-call $. Don't
  enqueue more than 1–2 test prompts per session. Don't trigger cron
  faster than its 2 h rate. Don't "fix" things by running the generator
  in a loop. The cron's text-LLM top-up (default `gpt-5-mini` via
  `expand_topic`) is a rounding error by comparison — those calls are
  cheap and bounded per tick by `TARGET_QUEUE_LENGTH`. The image-gen
  call is the one that costs. There is **no daily $ cap yet** (see
  [TODOS.md](TODOS.md)).
- **Don't re-introduce the S3 ObjectCreated trigger on `queue/`.** The
  queue was redesigned in [0.5.0.0] to be a curated buffer: items wait
  until cron or an admin **Run** / **Now** explicitly renders them. If
  you wire the trigger back, every enqueue becomes a render, the
  reorder UI stops mattering, and the OpenAI bill goes up. The
  generator handler explicitly logs+ignores stray S3 events for the
  same reason. New rendering triggers should be additional
  `lambda.invoke` callers (e.g. the future wake-button endpoint —
  PLAN §2 item 16), not S3 notifications.
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
