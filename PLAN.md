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
│       ├── generator.py            # S3 event + cron handlers; calls pipeline
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
8. **Generator Lambda.** Single Lambda with two triggers — S3
   ObjectCreated on `queue/` and EventBridge `rate(2 hours)`. Reserved
   concurrency = 1. On cron with empty queue, enqueues a `random`; the
   resulting S3 event drains it.
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

---

## 3. Decisions made

| Decision | Choice |
| --- | --- |
| Image model (v1) | OpenAI `gpt-image-1` at `1536×1024` |
| Auto-gen cron interval | every 2 hours |
| Device poll interval | 1 hour (configurable via `einkgenPollIntervalSeconds` CDK context; firmware `SLEEP_MAX_SECONDS` must match — QUICKSTART §3.12) |
| Prompt strategy | 10-entry random library (ARCHITECTURE §6) + a base prompt that specifies the panel and dither constraints |
| Aspect / resize policy | center-crop only (1536×1024 → 1200×825), no resampling for generated images |
| Bucket access | `current/*` and `web/*` public via CloudFront; `history/*` public for `processed.bmp` only (viewer-request function); rest accessed only via Lambdas |
| Device cadence | sleeps `min(next_check_after, SLEEP_MAX_SECONDS)`; manifest hint = next device-poll tick + 5 min buffer |
| Queue policy | strict FIFO, no coalescing |
| Queue trigger | S3 ObjectCreated → generator Lambda (concurrency = 1) |
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
- **Deploying the web app.** Today `cdk deploy -c includeWebAssets=true`
  picks up `web/dist/`. Two open questions: (a) should the web build run
  inside CDK bundling instead of as a separate `npm run build` step? (b)
  is the two-deploy workflow (deploy infra → read outputs → build web →
  redeploy) worth folding into a single `bin/deploy` script?
- **Concurrency safety on pop.** Reserved concurrency = 1 plus FIFO
  lex-sort is sufficient. If we ever raise concurrency, we'd need
  conditional `DeleteObject` (precondition on ETag) or move to SQS FIFO.
  Noted in `core/queue.py`.
- **Device-status response CORS.** The Lambda hardcodes
  `Access-Control-Allow-Origin: *` despite the endpoint being
  firmware-only and the API Gateway not configuring CORS. Not exploitable
  (no `Allow-Credentials`, token still required), but the code and the
  intent disagree. Trivial drop.
