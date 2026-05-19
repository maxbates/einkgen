# einkgen design plan

Why the system is shaped the way it is. Captures the implementation order
that got us here, the decisions that were locked in, and the questions
still open. For *what* is built, see [ARCHITECTURE.md](ARCHITECTURE.md);
for *how to deploy it*, see [QUICKSTART.md](QUICKSTART.md). Open follow-up
work lives in [TODOS.md](TODOS.md).

---

## 1. Repo layout

```
einkgen/
├── README.md                       # slim overview, links to the docs below
├── ARCHITECTURE.md                 # what the system is
├── PLAN.md                         # this file — why it's shaped this way
├── QUICKSTART.md                   # how to deploy it
├── CHANGELOG.md
├── TODOS.md
├── VERSION
├── pyproject.toml
├── config.toml
├── .env.example
├── src/einkgen/
│   ├── __main__.py                 # `python -m einkgen` → cli.main()
│   ├── cli/
│   │   ├── __init__.py             # root dispatcher
│   │   ├── status.py               # einkgen status
│   │   ├── history.py              # einkgen history
│   │   ├── queue.py                # einkgen queue {ls,rm,prompt,image}
│   │   └── local.py                # einkgen local {generate,convert,preview}
│   ├── core/
│   │   ├── generate.py             # OpenAI gpt-image-1 adapter + BASE_PROMPT + PROMPT_LIBRARY
│   │   ├── convert.py              # crop + grayscale + dither + 8-bit BMP encode
│   │   ├── publish.py              # write current/, archive history/, invalidate CF
│   │   ├── manifest.py             # manifest schema + next_check_after calc
│   │   ├── queue.py                # enqueue / pop_head / list / cancel (S3-prefix impl)
│   │   ├── pipeline.py             # one queue item → published frame
│   │   └── s3.py                   # thin boto3 wrapper used everywhere
│   └── lambdas/
│       ├── generator.py            # cron + render_now handlers; calls pipeline
│       ├── read_api.py             # GET /queue, /history, /status
│       └── device_status.py        # POST / (X-Device-Token)
├── web/                            # Vite + React SPA, vanilla CSS, no UI lib
│   ├── index.html
│   ├── src/
│   │   ├── App.tsx
│   │   └── tabs/{Queue,History,Device}.tsx
│   └── package.json
├── firmware/inkplate10/
│   ├── inkplate10.ino
│   └── secrets.h.example
├── infra/                          # CDK (bucket, CF, 3 Lambdas, 2 API Gateways, EventBridge, Secrets)
└── tests/
```

One CLI entrypoint, declared in `pyproject.toml`. `src/einkgen/core/` is
reused by both the CLI and the Lambdas — nothing model-related lives in
two places.

---

## 2. Implementation plan

Each milestone is independently useful. Through `[0.2.0.1]` everything
labelled 1–12 has shipped (see [CHANGELOG.md](CHANGELOG.md)); 13–15 are
future work.

1. **CLI skeleton + local convert.** `einkgen local convert <in> <out>` —
   center-crop, grayscale, Atkinson dither (+ FS as alt), 8-bit indexed
   BMP. Verify on the panel via microSD.
2. **Local generate.** `einkgen local generate "<prompt>" out.png` against
   `gpt-image-1` at 1536×1024 with `BASE_PROMPT` prepended.
3. **Local preview.** `einkgen local preview "<prompt>"` chains
   generate → convert and writes preview PNG.
4. **Publish primitive.** `core/publish.py` writes `current/` and
   `history/<id>/` and invalidates CloudFront.
5. **Inkplate firmware v0.** Hard-code an image URL, draw it on wake;
   verify Wi-Fi + render path.
6. **Manifest + conditional draw + 1h sleep cap.** Firmware compares
   sha256 in NVS, skips redraw if unchanged, sleeps
   `min(next_check_after, 1h)`.
7. **Queue.** `core/queue.py` over S3-prefix: `enqueue` / `pop_head` /
   `list` / `cancel`. CLI: `einkgen queue prompt|image|ls|rm`.
8. **Generator Lambda.** Single Lambda with two triggers — EventBridge
   `rate(30 minutes)` (cron) and `lambda.invoke` from the admin Lambda
   (`{"action": "render_now"}`, used by **Now** / **Run**). Reserved
   concurrency = 1. Each cron tick (a) tops the queue up to
   `TARGET_QUEUE_LENGTH=5` items by expanding random library topics
   through `generate.expand_topic` (text LLM, default `gpt-5-mini`)
   and enqueueing the expansions, then (b) renders the head.

   _Before [0.5.0.0] this Lambda also drained on S3 ObjectCreated for
   `queue/*.json`; the trigger was removed when the queue was
   redesigned into a curated buffer. See CHANGELOG [0.5.0.0]._
9. **Read-api Lambda.** Public endpoint serving `GET /queue`, `/history`,
   `/status`. No auth.
10. **Web app.** Vite + React SPA, three read-only tabs. Hosted from
    `web/` prefix behind CloudFront.
11. **Device status.** `einkgen-device-status` Lambda + `X-Device-Token`
    header. Firmware POSTs on each wake. Device tab renders.
12. **CloudWatch + manual error checks.** No SNS yet.
13. **(Future) Text/email input channel** — its own Lambda, its own auth,
    calls `queue.enqueue(...)`.
14. **(Future) Text/dashboard render mode** — replace the model call with
    a structured renderer (weather/calendar/etc.); same publish path.
15. **(Future) OTA firmware updates** via `firmware/`.
16. **Wake-button → instant advance** (landed in [0.6.0.0]).
    The implementation diverged from the original sketch: instead of
    firing `render_now` and asking the firmware to poll back a minute
    later, `POST /wake` now pops the head of a pre-rendered buffer
    (the new generated queue at `generated/<…>.json`) and re-points
    `current/manifest.json` at it synchronously. The firmware's next
    manifest fetch sees the new sha and redraws on the same wake. Cron
    pre-fills the buffer to depth 10 so a button press never has to
    block on a model call. A `render_one` async-invoke replenishes
    after each pop. The sha-mismatch branch debounces rapid presses
    (no pop until the device confirms it drew the previous one). See
    ARCHITECTURE §4b–§4c + CHANGELOG [0.6.0.0].

---

## 3. Decisions made

| Decision | Choice |
| --- | --- |
| Image model (v1) | OpenAI `gpt-image-1` at `1536×1024` |
| Auto-gen cron interval | every 30 minutes (was 2 h before [0.5.1.0]; flipping it is a one-line cdk + cdk.json change — see CHANGELOG [0.5.1.0] for cost/battery tradeoffs) |
| Device poll interval | 1 hour (configurable via `einkgenPollIntervalSeconds` CDK context; firmware `SLEEP_MAX_SECONDS` must match — QUICKSTART §3.12) |
| Prompt strategy | 10-entry random library (ARCHITECTURE §6) + a base prompt that specifies the panel and dither constraints |
| Aspect / resize policy | center-crop only (1536×1024 → 1200×825), no resampling for generated images |
| Bucket access | `current/*` and `web/*` public via CloudFront; `history/*` public for `processed.bmp` only (viewer-request function); rest accessed only via Lambdas |
| Device cadence | sleeps `min(next_check_after, SLEEP_MAX_SECONDS)`; manifest hint = next device-poll tick + 5 min buffer |
| Queue policy | two-priority buffer; key prefix `queue/0-…` (top) drains before `queue/1-…` (bottom); FIFO within each; no in-place mutation of objects. Reordering of existing items isn't supported — pick `at="top"` or `at="bottom"` at enqueue time. |
| Queue triggers | EventBridge `rate(30 minutes)` (cron — tops up prompt queue and buffers up to `MAX_RENDERS_PER_TICK` into the generated queue), and `lambda.invoke` with `{"action":"render_one"}` (`/wake` replenish), `{"action":"render_now"}` (admin Now), or `{"action":"render_item","item_id":...}` (admin Run). No S3 ObjectCreated drain since [0.5.0.0]. Reserved concurrency = 1 keeps them serial. |
| Cron top-up | Each tick (a) refills the prompt queue to ≥ 5 items via `generate.expand_topic` (text LLM, default `gpt-5-mini`), then (b) renders up to `MAX_RENDERS_PER_TICK = 2` items from the prompt queue into the generated buffer until the generated buffer reaches its target of 10. |
| Generated queue | New in [0.6.0.0]. Pre-rendered buffer between the prompt queue and history. Each marker at `generated/<iso_ts>-<history_id>.json` points at an existing `history/<id>/` archive. `/wake` pops the head; admin can skip (drop marker) or "Show this now" (set current + drop marker). |
| Display advance | `POST /wake` on the device-status Lambda. Sha-debounced advance: pop head of generated queue, `set_current_from_history`, fire `render_one` async. Cron does NOT touch `current/`. Admin **Now** / **Run** bypass the buffer for operator-driven immediacy. |
| Web app | read-only, React + Vite, three tabs (Queue / History / Device) |
| User input | CLI only in v1; text/email deferred behind a future channel-specific Lambda |
| Lambdas | 3 total: generator (writes), read-api (public reads), device-status (write status only) |
| Fronting | **API Gateway HTTP APIs** (read-api + device-status). Lambda Function URLs were the original plan but were blocked by AWS's account-level "block public access for Function URLs"; see CHANGELOG.md [0.2.0.1]. |
| Lambda architecture | **arm64 (Graviton2)**. Native to Apple Silicon dev machines, ~20% cheaper than x86_64, no `--platform linux/amd64` bundling quirks. |
| Pillow distribution | bundled into the generator's function zip directly. The original plan was a Klayers public Pillow layer, but the layer ARN became unavailable to the account during Phase 3 deploy; bundling removes the third-party dependency. |
| Generator async retries | **0** (both function and EventBridge target). Caps OpenAI cost-amplification from transient failures. |
| Cost cap | deferred (TODOS.md "Daily OpenAI cost cap") |
| History pagination | deferred |
| Battery SNS alerts | deferred |

---

## 4. Open questions

- **Operator IAM.** A single IAM user/role with full bucket access is the
  simplest CLI auth. Worth a scoped policy (`s3:PutObject` on `queue/*`,
  `s3:GetObject` on most paths, `s3:DeleteObject` on `queue/*`) so a
  compromised local profile can't, say, delete history. Decide before
  multi-operator usage.
- **Deploying the web app.** The two-deploy workflow (deploy infra →
  read outputs → build web → redeploy) is folded into
  `infra/scripts/deploy.sh` since the regressions documented in
  CLAUDE.md Hard rules. Still open: (a) should the web build run
  inside CDK bundling instead of as a separate `npm run build` step,
  so a bare `cdk deploy` always gets a correctly-configured bundle?
- **Concurrency safety on pop.** Reserved concurrency = 1 plus FIFO
  lex-sort is sufficient. If we ever raise concurrency, we'd need
  conditional `DeleteObject` (precondition on ETag) or move to SQS FIFO.
  Noted in `core/queue.py`.
- ~~**Device-status response CORS.**~~ Resolved in 0.6.5.0 — the
  firmware-only Lambda no longer advertises `Access-Control-Allow-Origin`.
