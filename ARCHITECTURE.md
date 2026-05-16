# einkgen architecture

How the system is shaped: target device, data flow, the queue, the image
pipeline, the AWS topology, and the threat model. For *how to deploy it*
see [QUICKSTART.md](QUICKSTART.md); for *why it's shaped this way* see
[PLAN.md](PLAN.md).

---

## 1. Target device — Inkplate 10

| Property | Value |
| --- | --- |
| Panel | 9.7" e-paper (ED097TC2) |
| Native resolution | **1200 × 825 px** (landscape) |
| Color depth | **8 grayscale levels (3-bit)** |
| Refresh time | 1.61 s full / 0.62 s fast / partial supported |
| MCU | ESP32 (Wi-Fi + BLE), 8 MB flash, 4 MB PSRAM |
| Storage | microSD slot, on-chip NVS |
| Power | USB-C or Li-Ion (3000 mAh option), 22 µA deep sleep |
| Peripherals | RTC, GPIO, I²C, SPI, EasyC/Qwiic |
| Image API (Arduino lib) | `drawImage(path, x, y, dither, invert)` — accepts BMP/JPG/PNG from SD, RAM, or `http(s)://` URLs |
| Programming | Arduino IDE (`Inkplate-Arduino-library`) or MicroPython |

**Image canvas we target:** 1200 × 825 px, 8-level grayscale, landscape, no rotation. Aspect ratio is ~1.455 : 1 (close to 3:2).

Sources: [Inkplate 10 overview](https://docs.soldered.com/inkplate/10/overview/), [Inkplate Arduino library](https://github.com/SolderedElectronics/Inkplate-Arduino-library), [features](https://inkplate.readthedocs.io/en/latest/features.html).

---

## 2. How the system works

```
   CLI ──┐            ┌────────────────────┐                     ┌──────────────────┐
  cron ──┤  enqueue ─▶│  queue (S3 prefix) │── S3 ObjectCreated ▶│ generator Lambda │──┐
 future ─┘            └────────────────────┘    (concurrency=1)  └──────────────────┘  │
                              ▲                                                        │
                              │                                                        ▼
                              │                                              ┌────────────────┐
                              │   read-only                                  │   S3 bucket    │
                       ┌─────────────────┐    ◀── public reads ──            │   + manifest   │
                       │  web app (SPA)  │    via read-api Lambda            └────────┬───────┘
                       │  3 tabs, public │                                            │
                       └─────────────────┘                                            │ CloudFront
                                                                                      ▼ HTTPS GET
                                                                              ┌──────────────┐
                                                                              │ Inkplate 10  │   wakes ≤ every 1h,
                                                                              │   firmware   │   pulls manifest,
                                                                              └──────────────┘   redraws if changed,
                                                                                                 POSTs status, sleeps
```

Writes (enqueue, generate, publish) come only from the CLI today; cron handles the empty-queue case. The web app is strictly read-only — three tabs that show the queue, the history, and the device's status — so a leaked URL can't burn any money. Future input channels (text, email, etc.) are deliberately deferred but the queue is the single contract they'll plug into.

A single generator Lambda drains the queue one item at a time (reserved concurrency = 1). Each new queue object fires an S3 `ObjectCreated` event that invokes the Lambda; cron is just another writer that drops a `random` item into the queue when nothing is pending.

The Inkplate runs a small sketch that, on every wake:
1. Joins Wi-Fi.
2. `GET /current/manifest.json` (CloudFront-cached, supports `If-None-Match`).
3. If `image_sha256` differs from the value in NVS, downloads `image.bmp` and calls `drawImage(..., dither=false)`.
4. POSTs `{battery, rssi, current_hash, fw_version}` to the device-status Lambda (shared-secret header).
5. Saves the new hash, schedules an RTC alarm for `min(manifest.next_check_after, now + 1h)`, deep-sleeps.

The server never pushes; it just guarantees `manifest.json` and `image.bmp` are fresh. The 1-hour sleep cap guarantees user-submitted prompts appear within ≤1h of being enqueued.

---

## 3. Inputs

All writes go through the queue (see §4). The queue is the only contract — adding new input channels later (text, email, a slack bot, whatever) means writing a thing that calls `queue.enqueue(...)`. Nothing else changes.

### CLI (only write path today)

Top-level structure: `status`, `history`, `queue …`, `local …`.

```
einkgen status                                    # latest device status (battery, RSSI, last hash)
einkgen history                                   # list recent published frames

einkgen queue ls                                  # list pending items
einkgen queue rm <id>                             # delete a pending item
einkgen queue prompt "<text>"                     # enqueue a prompt
einkgen queue image  <path>                       # enqueue an image (B&W passthrough)
einkgen queue image  <path> --prompt "<text>"     # restyle an image via gpt-image-2 edit

einkgen allowlist ls                              # list emails permitted to submit via email
einkgen allowlist add <email>                     # permit a sender
einkgen allowlist rm  <email>                     # revoke a sender

einkgen local generate "<text>" [<out.png>]   # call the model, save raw PNG
einkgen local convert  <in> <out.bmp>         # crop + grayscale + dither + encode
einkgen local preview  "<text>"               # generate + convert, save preview.png locally
```

`local *` never touches the bucket — pure dev/debug. Everything under `queue *` writes to S3 with the operator's IAM creds.

### Cron (only other writer)

- An EventBridge rule fires a thin entry in the generator Lambda **every 2 hours**.
- If the queue is empty, the Lambda enqueues a `{kind: "random"}` item; that drop triggers the normal S3-event path and the same Lambda invocation drains it.
- If the queue is non-empty, the Lambda processes **exactly one** head item per tick. This is a backstop for items stranded by a prior failed S3 delivery (e.g. a Lambda init crash that exhausted async retries). Steady-state, the S3 event has already drained them and the queue is empty by the time cron fires; one-per-tick keeps OpenAI cost bounded even if a real backlog builds up.

### Web app (read-only)

Public, AWS-hosted SPA. Three tabs: **Queue**, **History**, **Device**. No buttons, no forms, no writes anywhere. See §5.

### Inbound email (opt-in submission channel)

You can submit to the queue by emailing a configured address. Three modes:

- **Text only** — subject and first body line both contribute. When both carry text they are concatenated (subject, blank line, body); either alone is used as-is. Kind = `prompt`.
- **Image attached** — the image becomes the input frame, converted to B&W and published as-is. Kind = `image`.
- **Image + text** — image is fed to `gpt-image-2`'s edit endpoint with the prompt (same subject/body concatenation rule) as a restyle hint, then dithered and published. Kind = `image` with a prompt set.

**Cost protection.** A plain-text allowlist at `s3://<bucket>/config/email_allowlist.txt` lists every address permitted to submit. Senders not on the list get a friendly rejection email and **nothing is enqueued**; the reply never names other allowed addresses. Manage with `einkgen allowlist {ls,add,rm}` or edit the file directly. CDK seeds the initial allowlist on first deploy — see [QUICKSTART.md](QUICKSTART.md#email-submission-channel-optional).

**Sender authentication.** The inbound Lambda only trusts the `From:` address when SES's `Authentication-Results` header shows `spf=pass` or `dkim=pass` aligned with the From domain. Forged senders are dropped silently (no reply, since the From: can't be trusted). This is what makes the allowlist meaningful.

### SMS — explicitly skipped

AWS End User Messaging is the only AWS-native inbound-SMS path and isn't free: it requires a phone number (~$1–2/mo) and US 10DLC registration. Phone mail apps have native share-sheet support for "send image with caption to address X," so the inbound-email path covers the same UX without an extra service or recurring cost.

### Adding more channels

The queue API in `core/queue.py` is the seam: any future channel becomes a small Lambda that authenticates the sender and calls `queue.enqueue(...)`. Inbound email is the first such channel; a token-protected web form, an iOS Shortcut endpoint, or a Tailscale-only service could all follow the same pattern.

---

## 4. Queue

The queue is the single source of truth for "what image should appear next." Items are processed strictly FIFO, one at a time. No coalescing — if you enqueue three prompts in a row, the generator runs all three and the device shows the latest each time it wakes.

**Backing store: S3 prefix.** Each item is a JSON object at `s3://<bucket>/queue/<iso8601>-<ulid>.json`. The ULID is monotonic so lex-sorted keys = FIFO. Pop = `ListObjectsV2` (sorted) → `GetObject` → `DeleteObject`. No queue infra to provision.

**Trigger.** An S3 `ObjectCreated` notification on the `queue/` prefix fans into the generator Lambda. Lambda **reserved concurrency = 1** serialises drains so two events can't race on the same head.

If we ever outgrow the S3-prefix queue (multi-producer races, very high write rate), swap `core/queue.py` for an SQS FIFO or DynamoDB backing without touching anything else.

### Queue item schema

```json
{
  "id": "01HF7Z…",
  "enqueued_at": "2026-05-13T14:05:12Z",
  "source": "cli" | "cron" | "email" | "<future-channel>",
  "kind": "prompt" | "image" | "random",
  "prompt": "a foggy cliff at dawn",
  "image_s3_key": "queue/staged/abc123.jpg"
}
```

Field constraints by kind:

- **`prompt`** — `prompt` required, `image_s3_key` forbidden.
- **`image`** — `image_s3_key` required; `prompt` optional. With no prompt the upload is converted to B&W and published. With a prompt, the upload is fed to `gpt-image-2`'s edit endpoint, restyled per the prompt, then dithered and published.
- **`random`** — both forbidden; the generator picks from the prompt library and patches `prompt` in-place before publishing.

### Generator loop

```
on invoke (S3 event or 2h cron):
  if cron:
    if queue.empty():
      queue.enqueue({kind: "random"})  # falls through to the S3-event path
      return
    item = queue.pop_head()            # one item per tick, self-heal backstop
    if item is None: return
    process(item); return
  item = queue.pop_head()              # atomic via reserved concurrency = 1
  if item is None: return
  match item.kind:
    "prompt": img = model.generate(BASE_PROMPT + item.prompt)
    "image" if item.prompt: img = model.edit(item.image, BASE_PROMPT + item.prompt)
    "image":  img = s3.fetch(item.image_s3_key)
    "random": img = model.generate(BASE_PROMPT + random_choice(PROMPT_LIBRARY))
  processed = convert(img)
  publish(processed, source=item)
  archive(item)
```

### Cancel & idempotency

- `einkgen queue rm <id>` deletes the queue object before the generator picks it up.
- Each item has a stable `id`; archive on `history/<id>/` is idempotent on re-delivery.

---

## 5. Web app

A mostly-read-only dashboard. The public tabs have no buttons or forms — anything that would cost money or change state goes through the operator-only Admin tab, gated by a password. A leaked URL still grants nothing beyond visibility.

### Tabs

- **Queue.** Ordered list of pending items: kind, prompt (or image thumbnail), submitted-at, source. Public.
- **History.** Grid of every published frame, newest first. Each tile shows the dithered BMP (browsers render 8-bit indexed BMP natively) + the prompt + timestamp. Click for full size and metadata. Public.
- **Device.** Latest battery voltage and percent, Wi-Fi RSSI, last-seen timestamp, current `image_sha256` the device confirmed drawing, firmware version. Public.
- **Admin.** Password-gated form for submitting text prompts and image uploads to the queue. Sets an HMAC-signed `einkgen_admin` session cookie (HttpOnly, Secure, SameSite=Lax, Path=`/admin`, 90-day expiry) on successful login. Public viewers see only the password prompt.

### Stack

- **Frontend.** React + Vite SPA, no UI library, vanilla CSS. Built to static assets, hosted from S3 + CloudFront under the `web/` prefix.
- **Read backend.** A single `einkgen-read-api` Lambda fronted by an API Gateway HTTP API (CORS pinned to the CloudFront origin + localhost), serving:
  - `GET /queue`   → lists `queue/*.json` from S3.
  - `GET /history` → lists recent `history/<id>/manifest.json` entries.
  - `GET /status`  → latest `status/device-<id>.json`.
  The Lambda has IAM read-only access to the bucket. No writes, no API key for the API, no path that calls OpenAI.
- **Write backend.** A separate `einkgen-admin-api` Lambda fronted by its own HTTP API and **routed through the same CloudFront distribution** at `/admin/*` (so the session cookie is same-origin and SameSite=Lax just works). Routes:
  - `POST /admin/login`        — body `{"password": ...}`; on success returns 204 + a `Set-Cookie` HMAC-signed session token.
  - `GET  /admin/me`           — 200 if cookie is valid, 401 otherwise.
  - `POST /admin/logout`       — clears the cookie.
  - `POST /admin/queue/prompt` — `{"prompt": "..."}` → enqueues a text prompt (`source="admin"`).
  - `POST /admin/queue/image`  — `{"filename":..., "image_b64":..., "prompt":?}` → stages the image to `queue/staged/` and enqueues. Base64 keeps the Lambda multipart-free; API Gateway's 10 MB payload cap yields ~8 MB of decoded image.
  The Lambda has read access to `einkgen/admin_password` + `einkgen/admin_cookie_signing_key` and write access to `queue/*`. Reserved concurrency = 5.

---

## 6. Image pipeline

Server-side dithering (not on-device) so previews match what the panel actually shows and we can tune algorithms per content.

The display is **1200 × 825 px**, aspect **~1.4545 : 1**. `gpt-image-2` accepts arbitrary sizes provided both dimensions are multiples of 16 and total pixels are within 655,360–8,294,400 (see the OpenAI image-generation guide), so we ask for **1200 × 832** — the smallest valid size that exceeds the panel in both dims. The downstream step center-crops 7 px off the height with **zero resampling**. 1:1 pixel mapping, no anti-aliasing. We used to request 1536 × 1024 (inherited from `gpt-image-1`, which only offered fixed sizes); that generated 1,572,864 px and threw 37 % away.

Steps:
1. **Generate (or load)** the source image.
   - For generated images: request `1200 × 832` from `gpt-image-2` with the base prompt (below).
   - For uploads: take whatever the user provides.
2. **Fit to canvas.** Two paths, picked by `is_generated`:
   - **Generated** (`gpt-image-2` at 1200×832, composed for the whole canvas with no safe-area inset): **center-crop** to exactly 1200×825 (pixel-exact, no resampling, no AA) — just trims a 7-pixel sliver off the height.
   - **Uploaded** (any size, any aspect): **scale-fill** preserving aspect (CSS `background-size: cover`) + center-crop the overflow on the long axis. This is the default — a 4032×3024 phone photo is scaled to 1200×900 (using the larger of the two per-axis scale factors so the panel fills) and then cropped 37 px off the top and bottom. Filling the panel beats leaving white bars, and only a small slice on the long axis is lost.
3. **Grayscale + tone curve.** Luminance, optional gamma/contrast tweak (e-ink loses midtones).
4. **Dither** to 8 levels. Default **Atkinson** (high contrast, classic Mac look — best for the Inkplate's limited palette). Alternatives: Floyd–Steinberg, Bayer.
5. **Encode as 8-bit indexed BMP** with an 8-entry grayscale palette (~990 KB).
6. **Hash** (SHA-256) for the manifest.

Profiled cost on Lambda ARM64 1024 MB: dither ~2–3 s, OpenAI call ~52–55 s, publish <1 s. The pure-Python Atkinson stays — see [TODOS.md](TODOS.md) for the measurement.

### Base prompt (prepended to every generation)

```
Compose a single image at 1200×832 (landscape, ~1.44:1). It will be displayed on
a 1200×825 e-paper panel (a 7-pixel sliver trimmed off the height) and dithered
to 8 grayscale levels. The whole canvas is visible — there is no safe-area inset.
Use high-contrast tones, bold shapes, and clean edges — subtle gradients and fine
textures will not survive dithering. No text or watermarks. Subject:
```

The user/random subject string is appended to this base.

### Random-prompt library (`core/generate.py::PROMPT_LIBRARY`)

Used when cron fires with an empty queue. Ten entries: a mix of constrained styles and "model's choice" prompts so output stays varied.

1. **Geometric composition** — overlapping circles, squares, triangles; bold flat shapes; high contrast.
2. **Botanical illustration** — pen-and-ink style; a single plant or flower; scientific-diagram aesthetic.
3. **Pixel art scene** — 32×32 or 64×64 motif scaled up; chunky, low-detail.
4. **Architectural line drawing** — building, bridge, or interior; technical-drawing feel.
5. **Topographic / contour pattern** — abstract elevation lines or isobars.
6. **Vintage scientific diagram** — anatomy, astronomy, or mechanical schematic.
7. **Baby-friendly collage** — simple recognisable objects (animal, fruit, toy) arranged playfully.
8. **Abstract generative pattern** — flow fields, Voronoi, fractal noise.
9. **Portrait study** — single face, woodcut or charcoal feel.
10. **Model's choice** — open-ended: "anything striking that reads well in 8 grays."

`einkgen local preview "<text>"` writes the dithered output as PNG so we can eyeball it before pushing.

---

## 7. Manifest format

`s3://<bucket>/current/manifest.json`:

```json
{
  "version": 142,
  "generated_at": "2026-05-13T14:00:00Z",
  "image_url": "https://cdn.example.com/current/image.bmp",
  "image_sha256": "9f1c…",
  "image_bytes": 990123,
  "display": { "width": 1200, "height": 825, "levels": 8 },
  "next_check_after": "2026-05-13T16:05:00Z",
  "source": { "kind": "generated", "model": "gpt-image-2", "prompt": "…" }
}
```

`next_check_after = (time of next device-poll tick) + 5 min buffer`. The buffer covers the seconds-to-a-minute it takes to call the model and publish, so a device that exactly hits the hint won't arrive before the next image has landed. Firmware caps actual sleep at **1 hour by default** (`SLEEP_MAX_SECONDS` in [firmware/inkplate10/inkplate10.ino](firmware/inkplate10/inkplate10.ino)), so even if the server says "no need to check for 4 hours" the device still polls every hour. That cap is what bounds worst-case latency between an enqueue and the panel actually updating.

The device-poll tick is independent of the **EventBridge auto-gen cron** (still 2 h — §3). To change device polling, set `EINKGEN_POLL_INTERVAL_SECONDS` on the generator + inbound-email Lambdas (CDK context flag: `-c einkgenPollIntervalSeconds=...`) **and** edit the firmware's `SLEEP_MAX_SECONDS` so the sleep cap matches. Server-only changes get clamped; firmware-only changes are honoured but the manifest hint is wrong. See [QUICKSTART §3.12](QUICKSTART.md#312-optional-device-poll-interval) for the trade-off table.

---

## 8. S3 layout

```
s3://einkgen-<env>/
├── current/
│   ├── manifest.json        # what the device reads
│   └── image.bmp            # latest dithered frame
├── queue/
│   ├── 2026-05-13T14-05-12Z-01HF7Z….json   # pending items, lex-sortable
│   └── staged/abc123.jpg                    # media attached to image-kind items
├── inbound/
│   └── <ses-message-id>     # raw RFC 5322 messages from SES, deleted after processing
├── config/
│   └── email_allowlist.txt  # plain-text sender allowlist for inbound email
├── history/
│   └── 01HF7Z…/                # one folder per item id
│       ├── manifest.json       # has prompt, source, hash, timestamps
│       ├── original.png        # raw model output (or uploaded source)
│       └── processed.bmp       # what we sent to the device
├── firmware/
│   └── v0.1.0/inkplate.bin     # for future OTA
├── status/
│   └── device-<id>.json        # battery / RSSI / last-seen reports
└── web/
    └── index.html, assets/…    # the SPA build artefacts
```

Access policy:

| Prefix | Who reads | Who writes |
| --- | --- | --- |
| `current/*` | **public** via CloudFront — this is what the device fetches | generator Lambda only |
| `history/*` | public via CloudFront, but a viewer-request function gates the prefix to `processed.bmp` only — raw `original.png` uploads aren't reachable through the CDN | generator Lambda |
| `web/*` | public via CloudFront (the SPA) | the `BucketDeployment` construct on each `cdk deploy` |
| `queue/*`, `status/*`, `firmware/*` | read-api Lambda (IAM) | generator + device-status + CLI (IAM) |
| `inbound/*` | inbound-email Lambda only | SES (via receipt rule action), inbound-email Lambda (delete after processing) |
| `config/email_allowlist.txt` | inbound-email Lambda + CLI | CLI (`einkgen allowlist`), CDK seed on first deploy |

Browsers render 8-bit indexed BMP natively, so the History tab can `<img>` `processed.bmp` directly — no separate preview PNG needed.

---

## 9. AWS infrastructure

Four Lambdas (five with inbound email), one bucket, one CloudFront distribution, three API Gateway HTTP APIs. No SQS, no SNS, no DynamoDB.

- **S3 bucket `einkgen-<env>`** — single store: `current/`, `queue/`, `history/`, `status/`, `firmware/`, `web/`.
- **CloudFront distribution** — fronts the bucket via an Origin Access Control. `current/*`, `history/<id>/processed.bmp`, and `web/*` are publicly readable; the rest of the bucket is locked down at the bucket-policy level and accessed only via Lambdas with IAM.
- **Lambda `einkgen-generator`** — only writer of `current/` and `history/`. Triggered by:
  - **S3 ObjectCreated** on `queue/` (suffix `.json`) → drains head item.
  - **EventBridge** `rate(2 hours)` → enqueues a `random` if queue is empty (the resulting S3 event triggers the same Lambda).

  Reserved concurrency = **1** (serial drain). Async retries = **0** on both the function and the EventBridge target (caps OpenAI cost-amplification from transient failures). Reads `OPENAI_API_KEY` from Secrets Manager. ARM64 Graviton2, 1024 MB. Pillow is bundled into the function zip (no third-party Lambda layer).
- **Lambda `einkgen-read-api`** — public API Gateway HTTP API with CORS pinned to the CloudFront origin (plus `http://localhost:5173` for dev). Read-only IAM on the bucket. Routes: `GET /queue`, `GET /history`, `GET /status`. The web app's only backend.
- **Lambda `einkgen-device-status`** — API Gateway HTTP API with **no CORS** (firmware-only). Accepts `POST /` with an `X-Device-Token` header (validated against Secrets Manager). Writes `status/device-<id>.json`. Write-only IAM on the `status/` prefix. Reserved concurrency caps blast radius from abuse.
- **Lambda `einkgen-admin-api`** — API Gateway HTTP API attached to CloudFront as the `/admin/*` behavior (same origin as the SPA, so the session cookie can be SameSite=Lax). All routes live under `/admin/`. Validates the operator password against `einkgen/admin_password` on login (constant-time compare), mints an HMAC-SHA256-signed session cookie keyed by `einkgen/admin_cookie_signing_key`, and writes to `queue/*` (text prompt) or `queue/staged/*` (image) on success. Reserved concurrency = 5.
- **Lambda `einkgen-inbound-email`** *(opt-in, gated by the `einkgenInboundDomain` CDK context flag)*. S3 ObjectCreated on `inbound/*` triggers it; SES's receipt rule for the configured domain writes raw RFC 5322 messages there. Parses MIME, checks SES `Authentication-Results` for SPF/DKIM pass aligned with the From: domain, validates the sender against `config/email_allowlist.txt`, stages any image attachment under `queue/staged/`, calls `queue.enqueue(source="email")`, and sends a confirmation or rejection reply via SES. Scoped IAM: read+delete `inbound/*`, read `config/email_allowlist.txt`, write `queue/*`, `ses:SendEmail` constrained to the configured reply-From address. Reserved concurrency = 5.
- **Secrets Manager** — `einkgen/openai_api_key`, `einkgen/device_status_token`, `einkgen/admin_password`, `einkgen/admin_cookie_signing_key` (auto-generated by CDK on first deploy). Lambdas get scoped read.
- **EventBridge rule** — `rate(2 hours)` → `einkgen-generator` (cron entrypoint).
- **CloudWatch Logs** — 14-day retention per Lambda. One `MetricFilter` per Lambda emits an `ErrorLogCount-{name}` metric on the literal token `ERROR`. A CloudWatch dashboard (`einkgen-<env>`) plots invocations, errors, and duration p50/p99.

Everything is in a single CDK stack under [infra/](infra/).

---

## 10. Secrets & config

| Where | What |
| --- | --- |
| `.env` (gitignored) | Local CLI: `OPENAI_API_KEY`, `AWS_PROFILE`, `EINKGEN_BUCKET`, `EINKGEN_CDN_BASE` |
| AWS Secrets Manager | `einkgen/openai_api_key`, `einkgen/device_status_token`, `einkgen/admin_password`, `einkgen/admin_cookie_signing_key` — Lambdas only |
| `firmware/inkplate10/secrets.h` (gitignored) | Wi-Fi SSID/password and `DEVICE_STATUS_TOKEN` baked into the sketch |
| `config.toml` | Non-secret defaults: model (`gpt-image-2`), model size (`1200x832`), fit mode (`cover`), dither (`atkinson`), auto-gen cron (`2h`), device poll interval (`1h`, override via CDK context `einkgenPollIntervalSeconds`), next-check buffer (`5m`) |

Config resolution order: CLI flag → env var → `config.toml` → built-in defaults.

See [QUICKSTART.md](QUICKSTART.md) for how to populate the Secrets Manager values during deploy.

---

## 11. Inkplate firmware sketch

A single Arduino sketch in `firmware/inkplate10/`:
- Joins Wi-Fi (credentials in `secrets.h`, gitignored).
- HTTPS GET `manifest.json` from CloudFront, parse with `ArduinoJson`.
- Compare `image_sha256` to the value in NVS.
- If changed: `display.drawImage(manifestImageUrl, 0, 0, false, false)`; `display.display()`. Persist the new hash.
- POST `{battery_v, battery_pct, rssi, current_hash, fw_version}` to the device-status endpoint with `X-Device-Token: <DEVICE_STATUS_TOKEN>`.
- Compute wake target: `min(manifest.next_check_after, now + 1h)`. Set RTC alarm. Fallback: 1 hour if the manifest fetch failed.
- `esp_deep_sleep_start()`.

---

## 12. Security & threat model

Tabling each component against "what can go wrong":

| Surface | Worst case | Mitigation |
| --- | --- | --- |
| **Public CloudFront `current/*` and `history/*processed.bmp`** | Anyone reads the latest dithered image. | Acceptable — that's the device's read path. The CloudFront viewer-request function blocks `history/*original.png` from public reads. |
| **Read-api API Gateway endpoint** | Anyone reads queue/history/status metadata (incl. prompts). | Acceptable — public by design. CORS pinned to the CloudFront origin + `localhost:5173` to discourage casual abuse, but content is not sensitive. Lambda has read-only IAM so abuse can't escalate. |
| **Admin-api `/admin/*` endpoints (login + write)** | Attacker brute-forces the password and triggers unbounded OpenAI spend. | Single shared password in Secrets Manager (constant-time compare). Cookie is HMAC-signed with a CDK-auto-generated 64-byte key; tampering or forging requires the key. Reserved concurrency = 5 on the admin Lambda + reserved concurrency = 1 on the generator caps any successful attack at the cron rate. No daily $ cap yet (deferred — see [TODOS.md](TODOS.md)). Rotate the cookie key via `put-secret-value` to invalidate every outstanding session. |
| **Device-status API Gateway endpoint** | Attacker spams POSTs and fills `status/` with junk, racking up minor S3 costs. | `X-Device-Token` header validated against Secrets Manager via `hmac.compare_digest`. Wrong token → 401, no S3 write. `device_id` regex + 4 KB body cap + body-field allowlist. Lambda reserved concurrency caps blast radius. |
| **Generator Lambda** | If someone could invoke it with their own input, they could burn OpenAI spend. | No external trigger — only S3 ObjectCreated on `queue/` (`.json` suffix only — staged images can't trigger it) and EventBridge. The only way to write to `queue/` is operator IAM. Async retries = 0 so a transient failure can't multiply OpenAI cost. |
| **CLI / operator IAM** | Leaked AWS credentials → attacker spams the queue → unbounded OpenAI spend. | Scoped IAM policy (writes limited to `queue/`); standard credential hygiene (no committed `.env`); rotate on suspicion. No daily $ cap yet (deferred — see [TODOS.md](TODOS.md)). |
| **OpenAI API key** | Direct leak → attacker uses the key. | Stored in Secrets Manager, read only by the generator Lambda's role. Not present in the web app, the read-api Lambda, or the device. |
| **Web app (React)** | XSS via untrusted prompt strings rendered in the Queue/History tabs. | React escapes by default. We never `dangerouslySetInnerHTML`. Prompt sources are all trusted (CLI operator + built-in library). |
| **Firmware credentials** | Inkplate is stolen → Wi-Fi password + `DEVICE_STATUS_TOKEN` are readable from flash. | Acceptable. Token only writes status; Wi-Fi password is the same risk as any home IoT device. |
| **Supply chain** | Compromised Python or npm dep runs in Lambda or CLI. | Pin versions in `pyproject.toml` / `package-lock.json`; minimize deps (Pillow, boto3, openai, ULID; React, Vite). |
| **Manifest tampering** | Attacker writes a malicious `manifest.json` pointing the device at a payload. | Only the generator Lambda has write access to `current/*`. Bucket policy denies anonymous and operator writes to `current/*`. Device only fetches over HTTPS from CloudFront. |

Things we are explicitly **not** defending against in v1: cost-runaway from a compromised AWS account (deferred → cost cap is a future ask, [TODOS.md](TODOS.md)), DDoS of the public endpoints (AWS absorbs it; only S3 read amplification, which is bounded), and physical attacks on the Inkplate.
