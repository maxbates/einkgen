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
   CLI ───┐
   admin ─┤   enqueue                    cron tick (30 min)         per-pop replenish
   email ─┤  ───────▶  ┌──────────────┐  top up + render N      ┌──────────────────┐
   cron ──┘            │ prompt queue │ ──────────────────────▶│ generator Lambda │
                       │  (S3 queue/) │                         │ (concurrency=1)  │
                       └──────────────┘                         └─────────┬────────┘
                                                                          │ buffer_item:
                                                                          │  archive +
                                                                          ▼  enqueue marker
                                              ┌────────────────────────────────┐
                              advance / pop ◀─│  generated queue (S3 generated/)│
                              on /wake call   │  ~10 pre-rendered frames        │
                                              └─────────────┬───────────────────┘
                                                            │ set_current_from_history
                                                            ▼
                                                  ┌────────────────┐
                                                  │   S3 bucket    │
                                                  │   + manifest   │
                                                  └────────┬───────┘
                                                           │ CloudFront
                                                           ▼ HTTPS GET
                                                  ┌──────────────┐
                                                  │ Inkplate 10  │  POST /wake → advance,
                                                  │   firmware   │  GET manifest, redraw,
                                                  └──────────────┘  POST status, sleep
```

There are now **two queues**:

- The **prompt queue** at `queue/<…>.json` is the curated buffer of
  text submissions / image uploads waiting to be rendered. Cron's
  text-LLM top-up keeps it ≥ 5 deep so the next render step never
  starves.
- The **generated queue** at `generated/<…>.json` is the buffer of
  pre-rendered frames waiting to be *displayed*. Each marker points at
  an existing `history/<id>/` archive (a full render: dithered BMP,
  source PNG, history manifest); the device hasn't drawn it yet.
  Target depth is 10. Each cron tick refills the buffer all the way
  to that target in a single invocation (no per-tick render cap — the
  Lambda's 15-min timeout fits a worst-case 10-render cold-start
  comfortably). The `/wake` endpoint pops the head.

The cron tick — every 30 min by default — does two things: refill the
generated buffer to `TARGET_GENERATED_QUEUE_LENGTH` (drawing prompts
off the prompt queue, topping that queue up inline as it drains), then
leave the prompt queue at its floor so the SPA shows a sensible
"pending prompts" count between ticks. **Cron does NOT touch
`current/manifest.json`.** Display advancement is entirely driven by
`POST /wake`.

A single generator Lambda renders one item at a time (reserved
concurrency = 1). Cron, the per-wake `render_one` replenish, the
admin **Now** / **Run** overrides, and any future trigger all funnel
through that one serialised worker.

The Inkplate runs a small sketch that, on every wake (timer tick OR
WAKE-button press):
1. Joins Wi-Fi.
2. **`POST /wake`** with `{"current_sha256": "<nvs hash>"}` and the
   shared-secret token. The server compares against
   `current/manifest.json`:
     - sha **matches** + buffer non-empty → pop head, re-point
       current at it, fire `render_one` async to backfill, respond
       `advance` with the new sha. Subsequent GET manifest sees the
       update.
     - sha **mismatches** → device hasn't drawn the latest yet;
       respond `redraw`. **This is what debounces rapid presses** —
       no pop until the device confirms it caught up.
     - buffer empty → `queue_empty`. No advance, no synchronous
       OpenAI call.
3. `GET /current/manifest.json` (CloudFront-cached, supports
   `If-None-Match`).
4. If `image_sha256` differs from the value in NVS, downloads
   `image_url` (which points at `history/<id>/processed.bmp`) and
   calls `drawImage(..., dither=false)` after verifying the sha
   matches the manifest's claim.
5. POSTs `{battery, rssi, current_hash, fw_version}` to the
   device-status Lambda (`POST /`).
6. Saves the new hash, schedules an RTC alarm for
   `min(manifest.next_check_after, now + 1h)`, deep-sleeps until
   either the alarm fires or the WAKE button is pressed.

The server never pushes; it just guarantees `manifest.json` and the
referenced history bytes are fresh. The 1-hour sleep cap guarantees a
queued frame appears on the panel within ≤1 h of becoming the head
of the generated buffer.

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

### Cron (top up + render)

- An EventBridge rule fires the generator Lambda **every 30 minutes**.
- Each tick does two things, in order:
  1. **Top up.** If the queue holds fewer than `TARGET_QUEUE_LENGTH`
     (= 5) pending items, pick that many topics at random from
     `config/prompt_library.txt`, run each through a text-LLM
     expansion step (`generate.expand_topic`, default `gpt-5-mini`)
     to turn the short topic into a concrete image prompt, and
     enqueue the expansions at the bottom with `kind="prompt",
     source="cron"`. Text-LLM cost per expansion is a rounding error
     against one `gpt-image-2` call, so keeping the queue full is
     effectively free. A failed expansion falls back to enqueueing
     the raw topic — the queue still fills.
  2. **Render head.** Pop the head item (top queue first, then bottom),
     generate the image, publish.
- Cost is bounded by cadence: one image per tick = ~48 renders per
  day = ~$55/mo at gpt-image-2 medium pricing. A bigger queue doesn't
  change the rate; it just gives the operator more items to inspect /
  drop / run-now between ticks. Dialling up (`rate(15 minutes)`)
  costs ~$115/mo; going back to `rate(1 hour)` is ~$30/mo. The
  firmware doesn't need to be re-flashed for any of these — the
  manifest's `next_check_after` hint is what tells the device how
  often to poll, capped only when it would exceed
  `SLEEP_MAX_SECONDS = 1 h`.

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

## 4. Queues

The system has **two queues**. The prompt queue holds submissions
waiting to be rendered; the generated queue holds rendered frames
waiting to be displayed. The cron pipeline pulls from the prompt
queue and writes to the generated queue; the `/wake` device endpoint
pops from the generated queue and re-points `current/manifest.json`.

### 4a. Prompt queue (`queue/`)

The prompt queue is a **two-priority buffer**: cron / CLI / email /
admin all write to it; nothing renders until cron, `render_now`, or
the admin **Run** button explicitly says so. Items in the **top** queue always
drain before any item in the **bottom** queue; FIFO within each. No
coalescing.

**Backing store: S3 prefix.** Each item is a JSON object at
`s3://<bucket>/queue/<priority>-<iso8601>-<ulid>.json` where
`<priority>` is the literal character `"0"` (top) or `"1"` (bottom).
Lex-sorted `ListObjectsV2` is the queue order: all `0-…` items
precede all `1-…` items, ordered by their enqueue timestamp within
each priority. **S3 objects are never mutated after they're written.**
The earlier design used a `position: float` field and supported
arbitrary reordering, which required reading + rewriting individual
objects to move them; the user found that brittle for a buffer that
might be tweaked from a phone, so the redesign in [CHANGELOG 0.5.1.0]
swapped to fixed priorities encoded in the key.

Lambda **reserved concurrency = 1** on the generator serialises every
render (cron, async invokes, future wake-button hits) so two callers
can't race on the head.

If we ever outgrow the S3-prefix queue (multi-producer races, very high
write rate), swap `core/queue.py` for an SQS FIFO or DynamoDB backing
without touching anything else.

### Placement

- **Bottom insert** (`at="bottom"`, default): key prefix `queue/1-…`.
- **Top insert** (`at="top"`): key prefix `queue/0-…`.
- **No move-to-top.** Pick the right placement at enqueue time. If
  you need to render a specific pending item without waiting for it
  to reach the head, async-invoke the generator with
  `{"action": "render_item", "item_id": "..."}` (which is what the
  per-row **Run** button on the SPA Queue tab does).
- Items written before this key format existed (`queue/<iso_ts>-<ulid>.json`,
  no priority prefix) lex-sort *after* both new priorities — `"2026-…"`
  > `"1-…"` > `"0-…"` — so they drain as the queue tail. No migration
  needed.

### Queue item schema

```json
{
  "id": "01HF7Z…",
  "enqueued_at": "2026-05-13T14:05:12Z",
  "source": "cli" | "cron" | "email" | "admin" | "<future-channel>",
  "kind": "prompt" | "image" | "random",
  "prompt": "a foggy cliff at dawn",
  "image_s3_key": "queue/staged/abc123.jpg"
}
```

Field constraints by kind:

- **`prompt`** — `prompt` required, `image_s3_key` forbidden.
- **`image`** — `image_s3_key` required; `prompt` optional. With no prompt the upload is converted to B&W and published. With a prompt, the upload is fed to `gpt-image-2`'s edit endpoint, restyled per the prompt, then dithered and published.
- **`random`** — legacy kind, kept so items enqueued by older code still
  drain. New code paths never emit it: cron picks a topic from the
  prompt library, expands it via the text LLM, and enqueues
  `kind="prompt"` with the expansion baked in.

### Generator loop

```
on invoke:
  if event["action"] == "render_now":          # admin Now — sets current
    publish_item(queue.peek_head())            # archive + set current/
    queue.finalize(head)
    return
  if event["action"] == "render_item":         # admin Run — sets current
    publish_item(queue.get(event["item_id"]))
    return
  if event["action"] == "render_one":          # /wake replenish — buffer only
    buffer_item(queue.peek_head())             # archive + enqueue marker
    queue.finalize(head)
    return
  if cron (source=aws.events):
    top_up_prompt_queue(target=5)              # text-LLM expansions to ≥5
    while generated_queue.count() < 10 and tick_renders < MAX_RENDERS_PER_TICK:
        buffer_item(queue.peek_head())         # one render per loop iteration
    return
  # anything else (stray S3 event from the old trigger): log and ignore

publish_item(item):                            # admin path
  processed = _render(item)                    # generate → convert
  publish(processed, ...)                      # writes history/<id>/ AND current/

buffer_item(item):                             # cron / wake path
  processed = _render(item)
  manifest = archive_to_history(processed, ...)
  generated_queue.enqueue(item.id,             # marker carries sha + source
                          image_sha256=manifest.image_sha256,
                          ...)

_render(item):                                 # shared front half
  match item.kind:
    "prompt": img = model.generate(BASE_PROMPT + item.prompt)
    "image" if item.prompt: img = model.edit(item.image, BASE_PROMPT + item.prompt)
    "image":  img = s3.fetch(item.image_s3_key)
    "random": img = model.generate(BASE_PROMPT + prompt_library.random_prompt())
  return convert(img)
```

### Cancel, run, idempotency

- `einkgen queue rm <id>`, `DELETE /admin/queue/<id>`, and the Queue
  tab's **Remove** button all call `queue.cancel(id)` — deletes the S3
  object if it still exists, no-op otherwise.
- `POST /admin/queue/<id>/run` and the per-row **Run** button
  async-invoke the generator with `{"action": "render_item",
  "item_id": "<id>"}`. The generator fetches that specific item,
  renders it, and **sets current directly** (bypassing the generated
  buffer) — same intent as admin **Now**.
- There is **no move-to-top route**. Placement is decided at enqueue
  time (`at="top"` or `at="bottom"`).
- Each item has a stable `id`; archive on `history/<id>/` is idempotent
  on re-delivery.

### 4b. Generated queue (`generated/`)

The pre-rendered buffer. Each marker at `generated/<iso_ts>-<history_id>.json`
points at an existing `history/<id>/` archive:

```json
{
  "history_id": "01HF7Z…",
  "queued_at": "2026-05-13T14:05:12Z",
  "image_sha256": "abc…",
  "image_bytes": 990123,
  "source": { "kind": "generated", "model": "gpt-image-2", "prompt": "…" }
}
```

The marker is intentionally small — the dithered bmp lives under
`history/<id>/processed.bmp` and is already CDN-cached. Markers are
written by `core.pipeline.buffer_item` and consumed by `/wake`.

**FIFO, single priority, no in-place mutation.** Lex-sort of
`generated/*.json` is the queue order. Operations:

- `enqueue(history_id, sha, bytes, source)` — cron / `render_one`.
- `peek_head()` — `/wake` to find the next to display.
- `finalize(item)` — `/wake` after a successful advance.
- `cancel(history_id)` — admin **Skip** (`DELETE /admin/generated/<id>`)
  and the implicit drop on `POST /admin/show` (so "Show this now" on a
  buffered item both displays it and removes the duplicate marker).
- `count()` / `list()` — public `GET /generated`.

The `history/<id>/` archive **survives** a skip or a pop — the marker
is a lifecycle annotation on top of an item that's already in
history. Skipping just means "don't auto-display"; the operator can
still pin it later via **Show this now**.

### 4c. Display advance: `POST /wake`

The device-status Lambda handles two routes: the existing `POST /`
status heartbeat and `POST /wake`. Both are `X-Device-Token`
authenticated. `/wake` is the only way `current/manifest.json` moves
forward (other than admin **Now** / **Show this now**); cron does
not touch current.

The handler reads `current/manifest.json` and compares its
`image_sha256` to the device's reported `current_sha256`:

| device.sha vs manifest.sha | generated queue | response |
| --- | --- | --- |
| match (or no manifest yet) | non-empty | pop head, `set_current_from_history(history_id)`, async-invoke `render_one`, respond `advance` |
| match (or no manifest yet) | empty | respond `queue_empty` — wait for next cron tick |
| mismatch | (irrelevant) | respond `redraw` — device hasn't drawn the latest yet |

The mismatch branch is the **debounce**: rapid wake presses don't pop
multiple items because the second press still reports the old sha.
After the device fetches the new manifest and updates NVS, the next
wake matches and can advance again. Single-device deployments
naturally serialise this.

Both `advance` and `redraw` responses embed the manifest fields the
firmware needs to draw the next image — `image_url`, `image_sha256`,
`image_bytes`, `next_check_after`. Firmware feeds them straight into
the image GET so it never re-fetches `current/manifest.json` after a
`/wake` round-trip. This sidesteps CloudFront's 60–300 s cache on
that path (which historically returned the pre-advance manifest after
a `set_current_from_history` write, making the panel slow to update
after a wake-button press). `queue_empty` carries no manifest fields
— the firmware keeps drawing what it already has. A server rollback
(no embedded fields) drops the firmware back to the legacy
`fetchManifest` path automatically.

Concurrency: two simultaneous `/wake` calls (timer + button at the
same moment) can both reach the advance branch. Both write a new
manifest pointing at the same history id (idempotent — last write
wins, same content); the marker delete is racy but tolerant —
whichever loses the `DeleteObject` race sees a no-op. Reserved
concurrency on the generator (1) serialises any race on the
replenish render.

---

## 5. Web app

A mostly-read-only dashboard. The public tabs have no buttons or forms — anything that would cost money or change state goes through the operator-only Admin tab, gated by a password. A leaked URL still grants nothing beyond visibility.

### Tabs

- **Queue.** Ordered list of pending items: kind, prompt (or image thumbnail), submitted-at, source. Public. Logged-in operators get per-row **Run** / **Remove** buttons (Apple-Music-style icons) so the queue is curatable from any phone or laptop; the head item is marked with an `up next` chip. **Run** doesn't reorder the queue — it asks the generator to render that one item next-up via `render_item`. There is no per-row "move-to-top" affordance; placement is decided at enqueue time (Top / Bottom / Now on the Admin form).
- **History.** Grid of every published frame, newest first. Each tile shows the dithered BMP (browsers render 8-bit indexed BMP natively) + the prompt + timestamp. Click for full size and metadata. Public.
- **Device.** Latest battery voltage and percent, Wi-Fi RSSI, last-seen timestamp, current `image_sha256` the device confirmed drawing, firmware version. Public.
- **Admin.** Password-gated form for submitting text prompts and image uploads to the queue. Each form has three submit buttons — **Top** (insert at head of queue), **Bottom** (default — append), **Now** (insert at head + immediately render). Sets an HMAC-signed `einkgen_admin` session cookie (HttpOnly, Secure, SameSite=Lax, Path=`/admin`, 90-day expiry) on successful login. Public viewers see only the password prompt.

### Stack

- **Frontend.** React + Vite SPA, no UI library, vanilla CSS. Built to static assets, hosted from S3 + CloudFront under the `web/` prefix.
- **Read backend.** A single `einkgen-read-api` Lambda fronted by an API Gateway HTTP API (CORS pinned to the CloudFront origin + localhost), serving:
  - `GET /queue`   → lists `queue/*.json` from S3.
  - `GET /generated` → lists `generated/*.json` markers (the pre-rendered buffer).
  - `GET /history` → lists recent `history/<id>/manifest.json` entries.
  - `GET /status`  → newest `status/device-<id>.json` (single device, kept for back-compat with the SPA's Device tab).
  - `GET /devices` → every `status/device-<id>.json` newest-first, each merged with the device id and a string `last_modified`. Capped at `MAX_DEVICES_LIMIT = 200` so a runaway producer can't turn each public-SPA poll into O(devices) GetObjects. Empty list (200) when no reports exist.
  The Lambda has IAM read-only access to the bucket. No writes, no API key for the API, no path that calls OpenAI.
- **Write backend.** A separate `einkgen-admin-api` Lambda fronted by its own HTTP API and **routed through the same CloudFront distribution** at `/admin/*` (so the session cookie is same-origin and SameSite=Lax just works). Routes:
  - `POST /admin/login`        — body `{"password": ...}`; on success returns 204 + a `Set-Cookie` HMAC-signed session token.
  - `GET  /admin/me`           — 200 if cookie is valid, 401 otherwise.
  - `POST /admin/logout`       — clears the cookie.
  - `POST /admin/queue/prompt` — `{"prompt": "...", "at": "top"|"bottom"|"now"}` → enqueues a text prompt (`source="admin"`). `at` defaults to `"bottom"`; `"top"` jumps to head; `"now"` enqueues at top **and** async-invokes the generator so the new item renders immediately.
  - `POST /admin/queue/image`  — `{"filename":..., "image_b64":..., "prompt":?, "at":?}` → stages the image to `queue/staged/` and enqueues. Base64 keeps the Lambda multipart-free; API Gateway's 10 MB payload cap yields ~8 MB of decoded image. Same `at` semantics.
  - `POST /admin/queue/<id>/run` — async-invoke the generator with `{"action": "render_item", "item_id": "<id>"}` so that specific item renders next, without any reordering or in-place rewrite of S3 objects. The HTTP request returns 202; render runs off-request.
  - `DELETE /admin/queue/<id>` — cancel a pending item.
  - `POST /admin/show`         — `{"history_id": "..."}` → re-publishes an existing history frame as current. Reads `history/<id>/manifest.json`, then writes a new `current/manifest.json` whose `image_url` points back at `history/<id>/processed.bmp` and whose `image_sha256`/`image_bytes` carry over. No byte copy, no regenerate, no queue item, no OpenAI call. The next normal generation overwrites the manifest back to `current/image.bmp`. The manifest's `source.replayed_from` field carries the history id so the SPA can mark the "now showing" tile unambiguously even when two history items share a SHA-256.
  The Lambda has read access to `einkgen/admin_password` + `einkgen/admin_cookie_signing_key`, read+write+delete on `queue/*` (writes for enqueue, reads for `get(id)` + `move_to_top`, deletes for `cancel`), `ListBucket` scoped to `queue/*`, read access to `history/*`, read+write access to `current/*`, `cloudfront:CreateInvalidation`, and `lambda:InvokeFunction` on the generator (used by `at="now"` and `/run`). Reserved concurrency = 5.

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
Favor a bright, paper-white background with the subject rendered in strong darks
against it — e-ink looks best when most of the canvas is light. Avoid flooding
large areas with dark or muddy mid-grays. Use high-contrast tones, bold shapes,
and clean edges — subtle gradients and fine textures will not survive dithering.
No text or watermarks. Subject:
```

The user/random subject string is appended to this base.

### Topic library (`core/prompt_library.py`)

Used by cron's top-up step: each tick, if the queue is short of
`TARGET_QUEUE_LENGTH` pending items, the cron picks a topic at random
from the library, runs it through `generate.expand_topic(topic)` (a
text-LLM call — default `gpt-5-mini`, override via `EINKGEN_TEXT_MODEL`),
and enqueues the expansion as `kind="prompt", source="cron"`. The
expansion is concrete enough to drive the image model directly. We
expand at top-up time rather than at render time so the queue holds
human-readable prompts the operator can preview / reorder / drop, and
so text-generation variance can do its job (the same topic produces
different prompts each pick, which then yield different images).

The bank itself is operator-editable at runtime: it lives as a plain
text file at `s3://<bucket>/config/prompt_library.txt`, one **topic**
per line (the historical "one prompt per line" still works — the file
shape didn't change), edited from the SPA **Admin** tab, the
`einkgen prompts {ls,edit,reset}` CLI, or `aws s3 cp`. A 60-second
in-Lambda cache amortises the fetch across warm invocations. If the S3
file is missing or empty, `load()` falls back to the seed defaults
below so a fresh deploy never picks from an empty bank. The seed is
ten entries — a mix of constrained styles and "model's choice" so
output stays varied, even before the expansion step runs:

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

`version` is monotonically increasing and is the audit trail for who advanced the panel and when. Since 0.6.5.0 all `current/manifest.json` writes — `publish()`, `set_current_from_history()` (the `/wake` advance + `/admin/show`) — route through `_write_current_manifest_cas` in `core/publish.py`, which uses S3 `If-Match` / `If-None-Match` conditional PUTs with a 6-attempt retry loop. Two concurrent `/wake` advances both reading `version = N` can no longer both write `N+1`; the loser re-reads and bumps to `N+2`. Before 0.6.5.0 the read-modify-write was racy, but the visible blast radius was small (one history pop ignored, the next press got it right).

The device-poll tick and the **EventBridge auto-gen cron** are driven by the same value — `einkgenPollIntervalSeconds` in [infra/cdk.json](infra/cdk.json) (default `1800` = 30 min). One knob, one redeploy, no drift. Values ≤ 3600 are honoured by the firmware directly (its `SLEEP_MAX_SECONDS` is a cap on long sleeps, not a target); values > 3600 also need `SLEEP_MAX_SECONDS` raised in [firmware/inkplate10/inkplate10.ino](firmware/inkplate10/inkplate10.ino) before re-flash. See [QUICKSTART §3.12](QUICKSTART.md#312-change-the-render--poll-cadence-later) for the trade-off table.

---

## 8. S3 layout

```
s3://einkgen-<env>/
├── current/
│   ├── manifest.json        # what the device reads — points at history/<id>/processed.bmp post-/wake
│   └── image.bmp            # legacy path; written only by admin "Now"/"Run" overrides
├── queue/
│   ├── 0-2026-05-13T14-05-12Z-01HF7Z….json  # prompt queue, lex-sortable (priority 0 = top, 1 = bottom)
│   └── staged/abc123.jpg                    # media attached to image-kind items
├── generated/
│   └── 2026-05-13T14-10-00Z-01HF7Z….json    # pre-rendered buffer markers, FIFO; each points at history/<id>/
├── inbound/
│   └── <ses-message-id>     # raw RFC 5322 messages from SES, deleted after processing
├── config/
│   └── email_allowlist.txt  # plain-text sender allowlist for inbound email
├── history/
│   └── 01HF7Z…/                # one folder per item id
│       ├── manifest.json       # has prompt, source, hash, timestamps
│       ├── original.png        # raw model output (or uploaded source)
│       └── processed.bmp       # what we sent (or will send) to the device
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
| `current/*` | **public** via CloudFront — this is what the device fetches | generator Lambda (admin **Now**/**Run** paths) + device-status Lambda (`/wake` advance) + admin Lambda (`/admin/show`) |
| `history/*` | public via CloudFront, but a viewer-request function gates the prefix to `processed.bmp` only — raw `original.png` uploads aren't reachable through the CDN | generator Lambda |
| `generated/*` | read-api Lambda (`GET /generated`) | generator Lambda (enqueue on render); device-status (delete on `/wake` pop); admin Lambda (delete on skip / show) |
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
- **Lambda `einkgen-generator`** — writes `history/` (always), `generated/` (cron + `render_one` paths), and `current/` (admin **Now** / **Run** paths). Triggered by:
  - **EventBridge** `rate(30 minutes)` → refill the generated buffer all the way to `TARGET_GENERATED_QUEUE_LENGTH` (no display advance). The buffer-refill loop tops the prompt queue back up inline (via text-LLM expansion of random library topics) whenever it runs dry, so a fully-drained buffer can refill in a single invocation. Lambda timeout is 15 min — fits a worst-case 10-render cold start.
  - **`lambda.invoke`** from the device-status Lambda with `{"action": "render_one"}` (`/wake` replenish after a pop) — archives to `history/` + enqueues a `generated/` marker, no `current/` write.
  - **`lambda.invoke`** from the admin Lambda with `{"action": "render_now"}` (admin **Now** button) or `{"action": "render_item", "item_id": "..."}` (per-row **Run** button) — archives to `history/` AND sets as current, bypassing the generated buffer.

  No S3 ObjectCreated trigger — see §4. Reserved concurrency = **1** (serial drain across all triggers, including the new `render_one`). Async retries = **0** on both the function and the EventBridge target (caps OpenAI cost-amplification from transient failures). Reads `OPENAI_API_KEY` from Secrets Manager. ARM64 Graviton2, 1024 MB. Pillow is bundled into the function zip (no third-party Lambda layer).
- **Lambda `einkgen-read-api`** — public API Gateway HTTP API with CORS pinned to the CloudFront origin (plus `http://localhost:5173` for dev). Read-only IAM on the bucket. Routes: `GET /queue`, `GET /generated`, `GET /history`, `GET /status` (newest single device), `GET /devices` (every device, newest-first, capped at 200). The web app's only public backend.
- **Lambda `einkgen-device-status`** — API Gateway HTTP API with **no CORS** (firmware-only). Two routes, both with `X-Device-Token` shared-secret auth:
  - `POST /` writes `status/device-<id>.json` (battery / RSSI heartbeat).
  - `POST /wake` advances the display: reads `current/manifest.json`, compares the sha against the device's reported `current_sha256`, pops the head of `generated/` and points current at it via `set_current_from_history`, fires `render_one` async at the generator to refill the buffer. Sha-debounced (mismatch = "device hasn't redrawn yet, no pop").

  Reserved concurrency caps blast radius from abuse. IAM: read+write `current/*`, read `history/*`, list+read+delete `generated/*`, invoke generator, CF invalidation, secrets read, write-only `status/*`.
- **Lambda `einkgen-admin-api`** — API Gateway HTTP API attached to CloudFront as the `/admin/*` behavior (same origin as the SPA, so the session cookie can be SameSite=Lax). All routes live under `/admin/`. Validates the operator password against `einkgen/admin_password` on login (constant-time compare), mints an HMAC-SHA256-signed session cookie keyed by `einkgen/admin_cookie_signing_key`, and writes to `queue/*` (text prompt) or `queue/staged/*` (image) on success. Reserved concurrency = 5.
- **Lambda `einkgen-inbound-email`** *(opt-in, gated by the `einkgenInboundDomain` CDK context flag)*. S3 ObjectCreated on `inbound/*` triggers it; SES's receipt rule for the configured domain writes raw RFC 5322 messages there. Parses MIME, checks SES `Authentication-Results` for SPF/DKIM pass aligned with the From: domain, validates the sender against `config/email_allowlist.txt`, stages any image attachment under `queue/staged/`, calls `queue.enqueue(source="email")`, and sends a confirmation or rejection reply via SES. Scoped IAM: read+delete `inbound/*`, read `config/email_allowlist.txt`, write `queue/*`, `ses:SendEmail` constrained to the configured reply-From address. Reserved concurrency = 5.
- **Secrets Manager** — `einkgen/openai_api_key`, `einkgen/device_status_token`, `einkgen/admin_password`, `einkgen/admin_cookie_signing_key` (auto-generated by CDK on first deploy). Lambdas get scoped read.
- **EventBridge rule** `einkgen-generator-cron` — `rate(...)` driven by `einkgenPollIntervalSeconds` in [infra/cdk.json](infra/cdk.json) (default `rate(30 minutes)`). The same value also flows to the Lambda env var `EINKGEN_POLL_INTERVAL_SECONDS` so the device polls in step.
- **CloudWatch Logs** — 14-day retention per Lambda. One `MetricFilter` per Lambda emits an `ErrorLogCount-{name}` metric on the literal token `ERROR`. A second metric filter on the generator (`einkgen-<env>-buffer-empty-after-refill`) counts the literal token `BUFFER_EMPTY_AFTER_REFILL` — emitted by the generator at the end of any cron tick that finishes with generated-queue depth = 0. The alarm `einkgen-<env>-generated-queue-empty` pages via the existing `einkgenAlarmEmail` SNS topic after two consecutive empty ticks (catches the empty-prompt-library deadlock + chronic `expand_topic` failures). A CloudWatch dashboard (`einkgen-<env>`) plots invocations, errors, and duration p50/p99.

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

A single Arduino sketch in `firmware/inkplate10/`. Each wake (timer
RTC alarm OR `EXT0` WAKE-button press) runs `setup()` cold:

- Joins Wi-Fi (credentials in `secrets.h`, gitignored).
- Reads the previously-shown sha + low-battery flag from NVS.
- **`POST /wake`** with `{"current_sha256": "<nvs hash>"}` and the
  `X-Device-Token` header. The server uses this to decide whether to
  pop the generated buffer head (see §4c); the firmware doesn't act
  on the response body directly — it logs it for serial debugging
  and proceeds. A failed `/wake` is OK; the manifest-fetch path
  takes over.
- HTTPS GET `manifest.json` from CloudFront, parse with `ArduinoJson`.
- Compare `image_sha256` to the value in NVS.
- If changed: download `image_url` (now points at `history/<id>/processed.bmp`)
  into a PSRAM buffer, verify its SHA-256 matches the manifest's claim,
  `display.image.drawBitmapFromBuffer(...)`, optionally composite the
  low-battery overlay, `display.display()`. Persist the new hash + low
  flag.
- POST `{battery_v, battery_pct, rssi, current_hash, fw_version}` to
  the device-status endpoint with `X-Device-Token: <DEVICE_STATUS_TOKEN>`.
- Compute wake target: `min(manifest.next_check_after, now + 1h)`.
  Set RTC alarm AND arm `EXT0` for the WAKE-button GPIO so a press
  short-circuits the sleep. Fallback: 1 hour if the manifest fetch
  failed.
- `esp_deep_sleep_start()`.

---

## 12. Security & threat model

Tabling each component against "what can go wrong":

| Surface | Worst case | Mitigation |
| --- | --- | --- |
| **Public CloudFront `current/*` and `history/*processed.bmp`** | Anyone reads the latest dithered image. | Acceptable — that's the device's read path. The CloudFront viewer-request function blocks `history/*original.png` from public reads. |
| **Read-api API Gateway endpoint** | Anyone reads queue/history/status metadata (incl. prompts). | Acceptable — public by design. CORS pinned to the CloudFront origin + `localhost:5173` to discourage casual abuse, but content is not sensitive. Lambda has read-only IAM so abuse can't escalate. |
| **Admin-api `/admin/*` endpoints (login + write)** | Attacker brute-forces the password and triggers unbounded OpenAI spend. | Single shared password in Secrets Manager (constant-time compare). Cookie is HMAC-signed with a CDK-auto-generated 64-byte key; tampering or forging requires the key. Reserved concurrency = 5 on the admin Lambda + reserved concurrency = 1 on the generator caps any successful attack at the cron rate. No daily $ cap yet (deferred — see [TODOS.md](TODOS.md)). Rotate the cookie key via `put-secret-value` to invalidate every outstanding session. |
| **Device-status API Gateway endpoint** (`POST /` + `POST /wake`) | Attacker spams POSTs and fills `status/` with junk, racking up minor S3 costs OR triggers `/wake` advances + replenish renders, draining the generated buffer and burning OpenAI calls. | `X-Device-Token` header validated against Secrets Manager via `hmac.compare_digest` (both routes). Wrong token → 401, no S3 write, no `lambda:Invoke`. `/` body: `device_id` regex + 4 KB body cap + body-field allowlist. `/wake` body: hex-64 sha regex + 4 KB cap. Lambda reserved concurrency caps blast radius. Each `/wake` triggers at most one `render_one` (generator's reserved concurrency = 1 serialises), so cost amplification is bounded by the cron cadence on top. The mismatch-debounce in `/wake` also caps per-press effect: only one pop until the device confirms it caught up. |
| **Generator Lambda** | If someone could invoke it with their own input, they could burn OpenAI spend. | No external trigger. The four entry paths are: EventBridge cron, `lambda.invoke` from the admin Lambda (cookie-gated `Now` / `Run`), `lambda.invoke` from the device-status Lambda (`/wake` replenish, gated by `X-Device-Token`), and IAM-scoped operator writes to `queue/`. Reserved concurrency = 1 serialises them all. Async retries = 0 so a transient failure can't multiply OpenAI cost. **Spend observability:** a CloudWatch alarm fires when generator invocations exceed `einkgenDailyRenderCap` (default 100/24 h, ~$4/day) and pages an operator via the alarm SNS topic — set `einkgenAlarmEmail` to subscribe an inbox. The cap is observability-only today (the alarm doesn't auto-stop the cron); next step is a circuit-breaker Lambda that disables the EventBridge rule on alarm (TODOS Option B). |
| **CLI / operator IAM** | Leaked AWS credentials → attacker spams the queue → unbounded OpenAI spend. | Scoped IAM policy (writes limited to `queue/`); standard credential hygiene (no committed `.env`); rotate on suspicion. The generator-invocation alarm above also catches IAM-credential abuse: an attacker spamming `queue/` only matters once a render fires, and excess renders show up in the same 24 h Invocations metric the alarm watches. |
| **OpenAI API key** | Direct leak → attacker uses the key. | Stored in Secrets Manager, read only by the generator Lambda's role. Not present in the web app, the read-api Lambda, or the device. |
| **Web app (React)** | XSS via untrusted prompt strings rendered in the Queue/History tabs. | React escapes by default. We never `dangerouslySetInnerHTML`. Prompt sources are all trusted (CLI operator + built-in library). |
| **Firmware credentials** | Inkplate is stolen → Wi-Fi password + `DEVICE_STATUS_TOKEN` are readable from flash. | Acceptable. Token only writes status; Wi-Fi password is the same risk as any home IoT device. |
| **Supply chain** | Compromised Python or npm dep runs in Lambda or CLI. | Pin versions in `pyproject.toml` / `package-lock.json`; minimize deps (Pillow, boto3, openai, ULID; React, Vite). |
| **Manifest tampering** | Attacker writes a malicious `manifest.json` pointing the device at a payload. | Three Lambdas have scoped write access to `current/*`: the generator (admin **Now**/**Run** paths), the admin Lambda (`/admin/show`), and the device-status Lambda (`/wake` advance via `set_current_from_history`). All three either require an operator session (admin) or a valid `X-Device-Token` (device-status) and can only point `current` at an existing `history/<id>/processed.bmp` archive — they can't invent new image bytes inside `current/`. Bucket policy denies anonymous and operator writes to `current/*`. Device only fetches over HTTPS from CloudFront and verifies the SHA-256 of the downloaded bytes against the manifest's claim before drawing. |

Things we are explicitly **not** defending against in v1: cost-runaway from a compromised AWS account (we now observe it via the generator-invocation alarm above, but don't auto-stop spend — see TODOS Option B), DDoS of the public endpoints (AWS absorbs it; only S3 read amplification, which is bounded), and physical attacks on the Inkplate.
