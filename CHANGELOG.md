# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses a 4-digit version scheme (MAJOR.MINOR.PATCH.MICRO).

## [0.6.1.0] - 2026-05-18

### Fixed
- **WAKE-button latency from minutes to one round-trip.** The
  ``POST /wake`` advance response now embeds ``image_url``,
  ``image_sha256``, ``image_bytes`` and ``next_check_after`` from the
  freshly-published manifest, and the firmware feeds them straight
  into ``downloadVerifyAndDraw`` instead of issuing a follow-up
  ``GET current/manifest.json``. The follow-up GET hit CloudFront's
  60–300 s cache and reliably returned the pre-advance manifest, so
  the device saw ``storedHash == imageHash``, skipped the redraw, and
  the panel only updated on the next wake cycle (≤ 1 h with the
  firmware's ``SLEEP_MAX_SECONDS`` cap). Self-healed but the
  wake-button UX was "press → nothing → wait 30 min → image changes".
  The ``action=redraw`` branch carries the same fields so the same
  bypass applies after an admin **Show this now**. ``action=queue_empty``
  carries no manifest fields — firmware falls back to ``fetchManifest``
  and keeps drawing what it already has. A server rollback (no
  embedded fields) degrades cleanly to the legacy fetch path. Modules:
  [src/einkgen/lambdas/device_status.py](src/einkgen/lambdas/device_status.py),
  [firmware/inkplate10/inkplate10.ino](firmware/inkplate10/inkplate10.ino).

## [0.6.0.0] - 2026-05-18

### Added
- **Pre-rendered "generated queue" buffer between the prompt queue and
  history.** A new S3 prefix ``generated/`` holds markers, each pointing
  at an existing ``history/<id>/`` archive that has been rendered but
  not yet shown on the panel. Target depth is 10 (configurable via
  ``TARGET_GENERATED_QUEUE_LENGTH`` in
  [src/einkgen/lambdas/generator.py](src/einkgen/lambdas/generator.py)).
  Each cron tick refills the buffer all the way to that target in a
  single invocation — there's no per-tick render cap. The generator's
  Lambda timeout is raised to 15 min (Lambda max) so a worst-case cold
  start of 10 renders × ~55 s fits comfortably. Steady state is 0–1
  renders per tick (``/wake`` triggers its own per-pop replenish).
  Module: [src/einkgen/core/generated_queue.py](src/einkgen/core/generated_queue.py).

- **`POST /wake` on the device-status Lambda.** Body
  ``{"current_sha256": "<hex>"}``; the server compares against
  ``current/manifest.json``. Three branches:

  * **sha matches + buffer non-empty** → pop the head marker, point
    current at that history frame via ``set_current_from_history``,
    async-invoke the generator with ``render_one`` to backfill,
    respond ``{"action":"advance","manifest_sha256":...}``. The device
    sees a new sha on its subsequent manifest fetch and redraws.
  * **sha mismatch (device hasn't drawn the latest yet)** → respond
    ``{"action":"redraw","manifest_sha256":...}``. **This is the
    debounce**: rapid wake presses never pop more than one item until
    the device has actually drawn the previous pop.
  * **buffer empty** → ``{"action":"queue_empty"}``. Don't burn a fresh
    OpenAI call to invent a frame on the wake path — wait for the next
    cron tick to refill.

  Auth is the existing ``X-Device-Token`` shared secret. The firmware
  calls ``/wake`` on every wake (timer or WAKE button) before fetching
  the manifest; a failed ``/wake`` degrades cleanly to the legacy
  redraw-if-changed behavior. The 30-minute timer cadence (the same
  ``einkgenPollIntervalSeconds`` knob from 0.5.1.0) continues to drive
  the firmware's wake interval — the new bit is the WAKE-button press
  now advances immediately instead of waiting for the next timer tick.

- **Generator action ``render_one``** — direct-invoke payload
  ``{"action": "render_one"}``. Renders the head of the prompt queue
  into the generated buffer (no display advance). Fired by ``/wake``
  to replenish after a pop so steady-state depth stays at the target.

- **Admin API: ``DELETE /admin/generated/<history_id>``** — skip a
  buffered render. The marker is dropped from the buffer so the panel
  never auto-advances to it; the ``history/<id>/`` archive stays
  intact so the operator can still pin it later via **Show this now**.
  Same cookie-gated session as the rest of ``/admin/*``.

- **Read API: ``GET /generated``** — public FIFO listing of the
  buffered markers. Each item is a tiny JSON with ``history_id``,
  ``queued_at``, ``image_sha256``, ``image_bytes``, ``source`` —
  enough for the SPA Queue tab to render a tile (thumbnail comes from
  ``history/<id>/processed.bmp``).

- **SPA Queue tab** now shows two sections: **Up next on the device**
  (the generated buffer, with thumbnails) and **Pending prompts** (the
  prompt queue). Admin sees per-row **Show now** / **Skip** buttons on
  the generated section.

### Changed
- **``POST /admin/show`` now also drops the matching generated-queue
  marker** if one exists for the history id. "Show this now" on a
  buffered item both promotes it to current AND removes the duplicate
  from the up-next list. History remains untouched.

- **Cron no longer touches ``current/manifest.json``.** Display
  advancement happens entirely on ``/wake`` (timer wake or button
  wake). Cron's job is now strictly buffer-maintenance: top up the
  prompt queue via ``expand_topic`` and refill the generated buffer
  all the way to ``TARGET_GENERATED_QUEUE_LENGTH`` (the buffer-refill
  loop tops the prompt queue back up inline whenever it runs dry, so
  even a fully-drained buffer fills in one tick). The admin **Now** /
  **Run** overrides still bypass the buffer and set current directly
  (since the operator explicitly chose to display *this* thing right
  now).

- **Generator Lambda timeout raised 5 min → 15 min.** Cron's worst
  case is the cold-start fill (~10 renders × 55 s ≈ 9 min). Steady-
  state is 0–1 renders per tick.

- **Pipeline split.** ``einkgen.core.pipeline`` now exposes both
  ``buffer_item(item)`` (archive + enqueue marker, cron path) and
  ``publish_item(item)`` (archive + set as current, admin path).
  ``process_item`` remains as an alias for ``publish_item`` for
  back-compat with the older test suite. The shared front half
  (generate → convert → source-dict assembly) is in ``_render``.

- **Firmware now hits ``POST /wake`` on every wake** before fetching
  the manifest. The current sha (read from NVS) is sent in the body
  so the server can debounce. A failed ``/wake`` is logged and
  ignored — the existing manifest-fetch path takes over, so flaky
  networks don't brick the panel.

### Infra (CDK)
- **device-status Lambda** gets new IAM perms: ``s3:GetObject`` /
  ``s3:PutObject`` on ``current/*``, ``s3:GetObject`` on
  ``history/*``, ``s3:GetObject`` / ``s3:DeleteObject`` on
  ``generated/*`` (plus ``s3:ListBucket`` scoped to ``generated/*``),
  ``cloudfront:CreateInvalidation``, and ``lambda:InvokeFunction`` on
  the generator. New env var
  ``EINKGEN_GENERATOR_FUNCTION_NAME`` so ``/wake`` can fire
  ``render_one``. Timeout raised from 10 s → 20 s to cover the advance
  path's serial S3 + CloudFront calls.
- **HTTP API** gets a second route ``POST /wake`` alongside the
  existing ``POST /``. Both hit the same Lambda; dispatch is by
  ``rawPath`` inside the handler.
- **generator Lambda** gets write access to ``generated/*`` so the
  cron path can enqueue markers.
- **admin Lambda** gets ``s3:GetObject``/``s3:DeleteObject`` on
  ``generated/*`` (skip + show-removes-marker), plus ``ListBucket``
  scoped to include ``generated/*``.

### Storage
- New prefix ``s3://<bucket>/generated/`` for the buffer markers.
  Filtered out by both the prompt-queue listing and the failure
  breadcrumb listing — distinct purpose, distinct prefix.

## [0.5.1.0] - 2026-05-17

### Changed
- **Single cadence knob: ``einkgenPollIntervalSeconds`` in
  [infra/cdk.json](infra/cdk.json) (default ``"1800"`` = 30 min).**
  This one value now drives BOTH the EventBridge cron rate that
  fires the generator AND the manifest's ``next_check_after`` hint
  for the device. Before this change the two were independent — the
  cron rate was hardcoded as ``Duration.hours(2)`` in
  ``infra/lib/lambdas.ts`` and the device hint was a separate
  optional CDK context flag — which made it easy to push one without
  the other and end up rendering 4× faster than the device polled
  (or vice versa, polling 4× faster than cron rendered). Coupling
  them at construction time removes that footgun.

  Cost at 30 min: ~48 renders/day ≈ $55/mo at gpt-image-2 medium
  pricing (vs the original ~$15/mo at 2 h). Battery life on the
  Inkplate ~3–4 months (vs ~6–9 months at 1 h). The full table is
  in [QUICKSTART §1.7](QUICKSTART.md#17-pick-a-render-cadence-optional-but-think-about-it-now).

  To change after deploy: edit ``cdk.json`` + redeploy. ``cdk synth``
  rejects values < 60 or not divisible by 60 (EventBridge ``rate()``
  only accepts whole-minute schedules). Values > 3600 also need
  ``SLEEP_MAX_SECONDS`` raised in
  [firmware/inkplate10/inkplate10.ino](firmware/inkplate10/inkplate10.ino)
  in lockstep — see [QUICKSTART §3.12](QUICKSTART.md#312-change-the-render--poll-cadence-later).

- **Queue reorderability dropped.** The ``position: float`` field on
  every ``QueueItem`` and the ``move_to_top`` / ``POST /admin/queue/<id>/top``
  routes are gone. The queue is now a fixed two-priority buffer:
  ``"top"`` queue items always drain before ``"bottom"`` queue items;
  FIFO within each. **No S3 object is mutated after it's written** —
  the priority is encoded in the key (``queue/0-<…>.json`` for top,
  ``queue/1-<…>.json`` for bottom). The user-facing **Top** / **Bottom**
  / **Now** placement buttons on the Admin form still work exactly the
  same; only the per-row "move-to-top" affordance on the Queue tab is
  removed.

- **Per-row "Run" no longer reorders.** Instead of "promote item to head
  and render", it async-invokes the generator with a new
  ``{"action": "render_item", "item_id": "..."}`` payload that renders
  that specific item out of queue order, without touching anything else
  on disk. The item is finalized (deleted) on success.

### Added
- **Generator action ``render_item``** — direct-invoke payload
  ``{"action": "render_item", "item_id": "..."}``. Fetches the named
  item by id, renders it, and finalizes. No-op (with INFO log) if the
  id has already been drained. Used by the Queue tab's per-row **Run**
  button.

### Removed
- ``QueueItem.position`` field. Items still load if S3 has stale JSON
  carrying the field — it's just ignored.
- ``queue.move_to_top(id)`` helper.
- ``POST /admin/queue/<id>/top`` route (returns 404 now).
- ``adminMoveQueueToTop()`` from the typed SPA client.
- Per-row **Top** button on the SPA Queue tab.

### Migration
- **Items already on the queue at the time of deploy** keep their old
  keys (``queue/<iso_ts>-<ulid>.json``, no priority prefix). Lex sort
  puts them *after* both new priorities — ``"2026-…"`` > ``"1-…"`` >
  ``"0-…"`` — so they drain naturally as the queue tail over a handful
  of cron ticks. No migration script.
- **In-flight 0.5.0.0 items** with a ``position`` field in their JSON
  body load fine — the field is dropped on read; ordering comes from
  the key prefix instead.

## [0.5.0.0] - 2026-05-17

### Changed
- **Queue redesign — the queue is now a curated buffer, not a fire-and-forget
  pipe.** The S3 ObjectCreated trigger on `queue/` is gone; items enqueued
  by CLI, email, admin, or cron sit on the queue until either the cron
  tick or an explicit admin action renders them. This was the central
  request of the redesign: long queue, only render when needed.
  - **`queue.QueueItem`** gains a `position: float` field. `list()` and
    `peek_head()` sort by `(position, enqueued_at)`. New items at the
    bottom get `max + 1`; at the top, `min - 1`. Reorder is in-place —
    the S3 key never changes, so `cancel(id)` and enqueue timestamps
    survive moves. Items written before this field existed default to
    `position=0.0` so the rollout doesn't strand any in-flight item.
  - **`queue.enqueue(..., at="top"|"bottom")`** — new keyword.
    `"bottom"` is the default.
  - **`queue.move_to_top(id)`**, **`queue.get(id)`**, **`queue.count()`**
    — new helpers used by the admin API and SPA.

- **Generator Lambda — cron tick now does two things.** Every 2 h:
  1. Top up the queue to at least 5 pending items by picking a topic
     from the operator-editable prompt library and asking a text LLM
     (default `gpt-5-mini`, override via `EINKGEN_TEXT_MODEL`) to expand
     it into a concrete image prompt. The expansion is the queue item;
     image generation happens later when the item reaches the head.
     Text-generation variance is much higher than image-generation
     variance, so expanding once per item yields more diverse frames
     from the same topic list.
  2. Render the current head.

  A new direct-invoke path — payload `{"action": "render_now"}` — lets
  the admin API ask the generator to render the head immediately (used
  by the **Now** and **Run** buttons). Reserved concurrency = 1 keeps
  everything serial: a Now request fired mid-cron queues behind it and
  runs as soon as the tick returns.

- **Random prompt library is now a *topic* bank, not a *prompt* bank.**
  Behaviorally identical — entries are still one line each, still
  edited from the SPA Admin tab — but the description and seed
  examples are framed as topics (the cron expands each pick before
  enqueueing). The seed defaults still load the original 10 entries
  for the first deploy.

### Added
- **Admin API — `at` field on enqueue and three per-item routes.**
  - `POST /admin/queue/prompt` / `/image` accept `at: "top" | "bottom"
    | "now"` (default `"bottom"`). `"now"` enqueues at the top AND
    async-invokes the generator so the new item renders immediately.
  - `POST /admin/queue/<id>/top` — move an existing item to the head.
  - `POST /admin/queue/<id>/run` — move to head + invoke generator.
  - `DELETE /admin/queue/<id>` — remove a pending item.
  - New env var: **`EINKGEN_GENERATOR_FUNCTION_NAME`**, used by `now`
    and `/run` to target the generator Lambda. Without it, those routes
    still enqueue but skip the immediate render; the next cron tick
    picks the item up.

- **SPA Admin tab — three-button enqueue.** The single "Enqueue prompt"
  / "Upload image" buttons are replaced with **Top** / **Bottom** /
  **Now** clusters with Apple Music–style icons (insert-at-top,
  insert-at-bottom, play).
- **SPA Queue tab — per-row admin actions.** When logged in as admin,
  each queued item gets **Top** / **Run** / **Remove** buttons. The
  head item shows an `up next` chip.
- **`einkgen queue prompt --top`** and **`einkgen queue image --top`** —
  CLI parity with the Admin "Top" button.
- **`einkgen.core.generate.expand_topic(topic)`** — the text-LLM
  topic→prompt expansion used by the cron's top-up step.

### Removed
- **S3 ObjectCreated trigger on `queue/`.** The generator no longer
  auto-drains on enqueue. Items wait for cron, `render_now`, or the
  admin "Run" button. Lambda already-in-flight S3 notifications fired
  before the trigger was removed are explicitly ignored by the new
  handler (logged at INFO and dropped) so they can't accidentally
  drain items.

### Migration
- **Items already on the queue at the moment of deploy** lack a
  `position` field. They load with `position=0.0`, so they stay in
  the middle when mixed with new entries (new tops go negative, new
  bottoms go positive). They will render on the next cron tick or the
  first admin "Run" — no data loss, just a brief delay relative to the
  old auto-drain behaviour. If you want to drain them faster, hit the
  **Run** button on each from the Queue tab.

## [0.4.1.4] - 2026-05-17

### Added
- **"Recently rejected" feedback in the Admin tab.** When the generator
  drops a queue item via `PermanentItemError` (e.g. OpenAI's safety
  system returned `moderation_blocked`), it now writes a small
  breadcrumb to `s3://<bucket>/queue/failed/<recorded_at>-<id>.json`
  with the prompt, source, and reason. The SPA's Admin tab fetches
  these via a new `GET /admin/failures` endpoint and shows them as a
  compact "Recently rejected" panel — hidden entirely when empty, so
  the happy path stays clean. Self-clearing: anything older than 1
  hour is filtered out on read and best-effort deleted on the next
  write, so the prefix never grows. The breadcrumb write is
  best-effort — a failed notification can't block the queue drain. The
  Queue tab and CLI listings are unaffected because `queue.list()`
  excludes the `queue/failed/` prefix the same way it already excludes
  `queue/staged/`.

## [0.4.1.3] - 2026-05-17

### Fixed
- **Queue no longer pins on a prompt OpenAI rejects.** If a prompt
  tripped OpenAI's safety system (HTTP 400 `moderation_blocked`), the
  generator Lambda raised `BadRequestError`, Lambda's async-invoke retry
  treated it as transient, and the item stayed at the head of the queue
  forever — every subsequent S3 event redelivered the same blocked
  prompt and no further items drained. The pipeline now translates
  `openai.BadRequestError` into a new `PermanentItemError`; the
  generator handler catches that signal, logs the failure, finalizes
  the item, and continues draining. Retryable errors (network, 5xx,
  rate limits) still propagate so Lambda retries them as before.

## [0.4.1.2] - 2026-05-16

### Changed
- **BASE_PROMPT** now nudges the model toward bright, paper-white
  backgrounds with the subject rendered in strong darks against them,
  and explicitly discourages flooding large areas with dark or muddy
  mid-grays. E-ink panels look best when most of the canvas is light;
  the previous "high contrast, bold shapes" guidance didn't prevent
  generations from filling the frame with heavy gray fields that
  dithered into a muddy wash. The new wording is additive — the
  existing 8-grayscale / no-text / no-gradients guidance is preserved
  — so the change is a tone shift, not a style override. Mirrored in
  [ARCHITECTURE.md §6](ARCHITECTURE.md#base-prompt-prepended-to-every-generation).

## [0.4.1.1] - 2026-05-16

### Fixed
- **Queue / History / Device tabs loaded "forever" on the live site.**
  The SPA bundle currently in production was built without
  `VITE_READ_API_URL` / `VITE_CDN_BASE` set, so the runtime fallback
  (`http://localhost:3001`) was baked in and every API call from the
  browser went to a non-existent local host. Rebuilt the bundle against
  the live stack outputs and redeployed. No code changes to the SPA or
  the read-api; the production data was always reachable, the deployed
  client just couldn't find it.

### Added
- **`infra/scripts/deploy.sh`** — single safe redeploy path. Reads live
  API URLs from CloudFormation, rebuilds `web/dist` against them, fails
  fast if the resulting bundle still contains `localhost:` or doesn't
  reference the real read-api host, runs `cdk deploy` (no domain
  overrides — preserves the canonical `cdk.json` context), then chains
  into `verify-deploy.sh`. Replaces the fragile "remember to do §3.6
  before §3.7" recipe that broke twice. `--no-web` skips the SPA
  rebuild; `--no-verify` skips the post-deploy check (not recommended).
- **`infra/scripts/verify-deploy.sh`** — post-deploy smoke test.
  Exercises the four API surfaces (read-api direct, admin-api direct +
  via CloudFront, device-facing manifest + image), the SPA shell, and
  the SPA bundle's integrity (no `localhost:`, refs the real read-api
  host, refs the CDN host); also sweeps the last 30 min of ERROR-level
  log lines across all four Lambdas. Curl-only, exits non-zero on any
  fail. Run after every deploy — `deploy.sh` chains it automatically.

### Changed
- [CLAUDE.md](CLAUDE.md) and [QUICKSTART.md](QUICKSTART.md): the
  "Redeploy" instructions now point at `deploy.sh` as the canonical
  path; the manual `cdk deploy` recipe is preserved below it for
  troubleshooting. §3.6, §3.7, and §3.8 are collapsed into a single
  "build + deploy + verify" section.

## [0.4.1.0] - 2026-05-16

### Added
- **"Show this now" — re-display any past frame on the device without
  re-generating.** A new `POST /admin/show` admin route takes a
  `{"history_id": "..."}` and writes a fresh `current/manifest.json`
  whose `image_url` points back at `history/<id>/processed.bmp` (no
  byte copy, no OpenAI call, no queue item). The device picks it up
  on its next poll. The History tab now shows a small "Now showing"
  eye badge on whichever tile is currently being drawn, and the
  details modal exposes a **Show this now** button for logged-in
  operators. Implemented in
  [`set_current_from_history`](src/einkgen/core/publish.py) +
  [`_handle_show`](src/einkgen/lambdas/admin_api.py); the manifest
  carries `source.replayed_from = <id>` so the SPA can resolve the
  current tile unambiguously even when two history items share a
  SHA-256. **Permission change:** the admin Lambda now also has
  `s3:GetObject` on `history/*`, `s3:GetObject`+`s3:PutObject` on
  `current/*`, and `cloudfront:CreateInvalidation` for the
  distribution — see the comment in [infra/lib/lambdas.ts](infra/lib/lambdas.ts)
  for why this stays within the existing operator-trust boundary.

## [0.4.0.6] - 2026-05-16

### Added
- **Random-pick prompt library is now operator-editable at runtime.**
  Previously the 10-entry `PROMPT_LIBRARY` was hardcoded in
  `core/generate.py`; changing it required a redeploy. The bank now
  lives at `s3://<bucket>/config/prompt_library.txt` (one prompt per
  line, `#` comments ignored) and is edited from three places:
  - the SPA **Admin** tab — a textarea with Save and "Reset to
    defaults" buttons, behind the existing 90-day session cookie;
  - the CLI: `einkgen prompts {ls,edit,reset}` — `edit` opens
    `$EDITOR` on the current bank;
  - directly via `aws s3 cp` or the AWS console for ad-hoc tweaks.
  A 60-second in-Lambda cache amortises the fetch across warm
  invocations, mirroring the email-allowlist pattern. Missing or
  empty file falls back to the 10 seed defaults baked into
  `core/prompt_library.py::DEFAULTS`, so a fresh deploy never picks
  from an empty bank. New admin API routes: `GET /admin/prompts`,
  `PUT /admin/prompts`, `POST /admin/prompts/reset`.
### Changed
- **Uploaded images now scale-fill the panel instead of scale-fitting with
  white bars.** `_fit_to_canvas` (the upload path, `is_generated=False`)
  scales by the *larger* of the two per-axis ratios (CSS `background-size:
  cover` semantics) and center-crops the overflow on the long axis. A
  4032×3024 phone photo now lands as 1200×900 → center-crop 37 px off the top
  and bottom → 1200×825 filling the whole panel, instead of being scaled to
  1100×825 with ~50 px white bars on either side.
- **Generator now asks `gpt-image-2` for 1200×832 instead of 1536×1024.**
  `gpt-image-2` accepts arbitrary sizes when both edges are multiples of 16
  (`gpt-image-1` only offered fixed sizes — 1024×1024, 1536×1024, 1024×1536 —
  which is why we were stuck on 1536×1024 even after upgrading the model).
  1200×832 is the smallest valid size that exceeds the 1200×825 panel in both
  dimensions, so the `is_generated=True` center-crop now just trims a 7-pixel
  sliver off the height instead of cropping 17 % off the height *and* 336 px
  off the width — the model used to spend 37 % of its output on pixels we
  threw away. Net effects: meaningfully cheaper per call (image-output tokens
  scale with pixel count), faster generation, and the model composes for the
  panel's aspect (1.4423 vs 1.4545, 0.84 % off) instead of for 3:2.
  `BASE_PROMPT` updated to drop the "centered safe area" concept — the whole
  canvas is now visible. See ARCHITECTURE §6.

## [0.4.0.5] - 2026-05-16

### Added
- **`shortcuts/README.md` — iPhone / Siri shortcut walkthroughs.**
  Two paths for submitting a prompt from the phone via *"Hey Siri,
  einkgen."*: a 2-action email shortcut that targets the existing
  inbound-email Lambda (recommended when [QUICKSTART §3.10](QUICKSTART.md#310-optional-email-submission-channel)
  is set up), and a 4–8-action HTTP shortcut that performs the
  `POST /admin/login` → `POST /admin/queue/prompt` admin API dance with
  the password embedded in the shortcut. Includes a `curl` sanity check
  so the endpoints can be verified before building the shortcut, plus
  rotation, sharing, and troubleshooting notes. Docs only — no code or
  CDK changes; no `cdk deploy` required.

## [0.4.0.4] - 2026-05-16

### Fixed
- **`einkgen.link` and inbound email restored after a flag-less
  redeploy wiped them.** The `0.4.0.3` redeploy (the CDK 2.254.0 bump
  to retire `nodejs20.x`) was run without `-c einkgenSiteDomain=...`
  / `-c einkgenInboundDomain=...`, so CloudFormation deleted the ACM
  cert, both CloudFront aliases, the apex A + AAAA Route 53 records,
  the MX record, all three DKIM CNAMEs, the SES domain identity, the
  inbound-email Lambda, and the catch-all receipt rule. The site
  stopped resolving (no A record) and email stopped being received.
  This is the **second** time this footgun has fired. Re-issued the
  cert, re-added the alias + A/AAAA / MX / DKIM records, re-created
  the inbound Lambda + SES identity + rule set, and re-activated the
  rule set. The orphan `einkgen-inbound` rule set left behind by the
  failed delete (it was active so SES refused to remove it) was
  deactivated and deleted before the redeploy so the new rule set
  could take its name without collision.

### Changed
- **`einkgenSiteDomain` and `einkgenInboundDomain` are now defaults
  in [infra/cdk.json](infra/cdk.json) `context`**, both set to
  `einkgen.link`. From this commit forward, a bare
  `cdk deploy` (no `-c` flags) preserves the live domain wiring on
  every redeploy. CLI overrides still win, so forkers can pass
  `-c einkgenSiteDomain=mydomain.com` (or `-c einkgenSiteDomain=`
  to disable). The old "remember the right flags" workflow was the
  proximate cause of the outage above; baking the defaults into
  `cdk.json` makes the safe path the default path. Added a Hard
  rule in [CLAUDE.md](CLAUDE.md) and an "Important" callout in
  [QUICKSTART.md §3.10](QUICKSTART.md#310-optional-custom-domain-for-the-site)
  explaining the trap and why the defaults are sticky.

## [0.4.0.3] - 2026-05-16

### Changed
- **CDK bumped from 2.170.0 to 2.254.0 to retire Node.js 20.x from the
  stack's auto-generated Lambdas.** AWS is ending support for the
  `nodejs20.x` Lambda runtime on April 30, 2026. Two CDK-managed
  custom-resource Lambdas in this stack were running on `nodejs20.x`:
  the `AwsCustomResource` singleton that seeds
  `config/email_allowlist.txt` on first deploy of the inbound-email
  construct, and the `LogRetention` provider that backs the
  `logRetention` prop on the inbound-email Lambda. In aws-cdk-lib
  2.254.0 both resolve via `Runtime.NODEJS_LATEST` (now
  `nodejs22.x`), so the next `cdk deploy` migrates them off the
  deprecated runtime with no source-level changes. The user-facing
  Lambdas (generator / read-api / device-status / inbound-email /
  admin-api) are Python 3.12 and unaffected. CLI bumped from 2.170.0
  to 2.1122.0 to match.

## [0.4.0.2] - 2026-05-16

### Fixed
- **Uploaded images are now scale-fit to the panel, not center-cropped.**
  Previously, any upload larger than 1200×825 in both dimensions was passed
  through `_fit_to_canvas`'s center-crop branch — so a 4032×3024 phone photo
  ended up as the middle 1200×825 slice with most of the image discarded.
  `convert()` now takes an `is_generated` flag: `True` (set by the generator
  paths in `core/pipeline.py` and `cli local preview`) keeps the zero-resampling
  center-crop for `gpt-image-2`'s 1536×1024 outputs; `False` (the default and
  what every upload now hits) scale-fits while preserving aspect, then pads
  with white. See ARCHITECTURE §6 step 2 for the updated wording.

## [0.4.0.1] - 2026-05-16

### Fixed
- **Cron self-heals stranded queue items.** When the 2 h auto-gen cron fires
  and the queue is non-empty, the generator Lambda now processes exactly
  one head item (was: no-op). Previously, an item enqueued while the
  generator was failing (e.g. a deploy briefly stuck with the
  synth-only-stub asset, an OpenAI outage longer than Lambda's async-retry
  budget, or a per-item pipeline bug) could be stranded forever — S3
  ObjectCreated retries exhaust within 6 h and the cron's old branch
  never touched a non-empty queue. One-per-tick keeps OpenAI cost bounded
  even with a real backlog; steady-state, the S3 event has already drained
  the queue by the time cron fires.

## [0.4.0.0] - 2026-05-15

The SPA grows an **Admin tab**. The operator can now submit text prompts and
upload images straight from the dashboard on a laptop or phone — no laptop
CLI required. Public viewers continue to see exactly what they saw before
(read-only Queue / History / Device tabs).

### Added
- **`einkgen-admin-api` Lambda.** Operator-only write endpoints behind a
  shared-password login:
  - `POST /admin/login`        — exchange password for an HMAC-signed
    session cookie (HttpOnly, Secure, SameSite=Lax, Path=`/admin`, 90-day
    expiry).
  - `GET  /admin/me`           — session probe used by the SPA to decide
    whether to show the login form or the admin panel.
  - `POST /admin/logout`       — clears the cookie.
  - `POST /admin/queue/prompt` — enqueue a text prompt (`source="admin"`).
  - `POST /admin/queue/image`  — base64-encoded image + optional restyle
    prompt; stages the image to `queue/staged/` and enqueues
    (`source="admin"`).
- **`einkgen.core.admin_cookie`.** HMAC-SHA256 cookie sign/verify with
  schema versioning, expiry, and `hmac.compare_digest` for forgery
  resistance. Used by the admin Lambda only.
- **Two new Secrets Manager secrets:**
  - `einkgen/admin_password` — operator-set, placeholder on first deploy
    (the admin Lambda refuses to authenticate while the placeholder is
    still in place, so a fresh stack isn't briefly world-writable).
  - `einkgen/admin_cookie_signing_key` — auto-generated by CDK
    (`generateSecretString`, 64 chars). Rotate to invalidate every
    outstanding admin session.
- **CloudFront `/admin/*` behavior.** Same-origin routing to the admin HTTP
  API so the session cookie can be SameSite=Lax — Safari and Firefox both
  block third-party cookies even with SameSite=None, which would have
  locked out the SPA on those browsers. Cache disabled, all viewer
  headers (Cookie, Authorization, body) forwarded to origin.
- **Web SPA: Admin tab.** Password form when not authenticated; otherwise
  a textarea for prompts, a file picker for image uploads (with an
  optional restyle prompt field), session expiry hint, and a logout
  button.
- **QUICKSTART updates.**
  - §1.5 — pick an admin password.
  - §3.5 — third `put-secret-value` step for `einkgen/admin_password`.
  - §3.8 — new smoke-test line for `GET /admin/me` (expects 401).
- **`einkgen-admin-api` integrated into observability** (CloudWatch error
  metric filter + dashboard) and `infra/scripts/check-errors.sh`.

### Security
- Admin Lambda explicitly rejects the `REPLACE_ME_POST_DEPLOY` placeholder
  with a 503 `not_configured` — a forgotten §3.5 step can't be exploited
  to log in.
- `Cache-Control: no-store` on every admin response so a 401 can't be
  cached by an intermediate.
- Cookie path-scoped to `/admin` — never sent to the public read API or
  the `current/` / `history/` paths on the same origin.

## [0.3.4.3] - 2026-05-15

### Changed
- **Test-suite bootstrap now uses `uv` instead of bare pip.** `CLAUDE.md`
  documents `uv run --extra dev pytest` as the canonical way to run the
  test suite, and warns away from `pip install -e ".[dev]"` + bare
  `pytest`. Without this, every fresh worktree (and every Claude Code
  session that landed in one) re-downloaded Pillow + moto + boto3 etc.
  from PyPI because the system pip cache was empty and the system Python
  on macOS dev boxes doesn't satisfy `requires-python >=3.11`. `uv`
  auto-syncs `.venv/` from `pyproject.toml` and reuses a global wheel
  cache, so a fresh worktree boots in seconds after the first install.
  Lockfile (`uv.lock`) is now committed so the resolved dependency set
  is reproducible across worktrees and machines.

## [0.3.4.2] - 2026-05-15

### Fixed
- **Firmware now compiles.** `drawBatteryOverlay()` declared local
  `const uint16_t BLACK = 0;` and `const uint16_t WHITE = 7;` for
  INKPLATE_3BIT colour values, but the Inkplate Arduino library's
  `defines.h` `#define`s `BLACK 1` and `WHITE 0` at the include level.
  Those macros expanded inside the function before the constants were
  parsed, producing `const uint16_t 1 = 0;` and a compile error.
  Renamed the locals to `INK_BLACK` / `INK_WHITE`. Latent since
  v0.3.2.0 — surfaced when the v0.3.4.0 WAKE-button work prompted a
  hardware re-flash and the overlay code hit the preprocessor for the
  first time.

## [0.3.4.1] - 2026-05-15

### Changed
- **Email confirmation echoes the captured prompt back to the sender.** When
  an inbound submission is accepted, the "submission queued" reply now
  includes a `Prompt:` section quoting the cleaned prompt text (subject +
  body merged, signature stripped). Lets the sender verify what the parser
  actually captured before the generator runs, instead of waiting for the
  resulting image to find out their subject line got dropped. Image-only
  submissions are unchanged — no `Prompt:` section unless a restyle hint
  was provided.

## [0.3.4.0] - 2026-05-15

### Added
- **Firmware: WAKE button forces an immediate poll.** The Inkplate's WAKE
  button (GPIO 36, active-low) is now configured as an EXT0 deep-sleep wake
  source alongside the existing timer. Pressing it during sleep ends the
  sleep early; `setup()` runs as usual, which already polls the manifest and
  redraws on every wake — so the button becomes a "refresh now" affordance
  without any new code path. Boot log also prints the wake cause
  (`wake-button` / `timer` / `reset-or-power-on`) so it's obvious in serial
  output why the device came up. The 60-second `SLEEP_MIN_SECONDS` floor on
  every wake still applies, so mashing the button gets you one refresh, not
  a request loop.

## [0.3.3.0] - 2026-05-15

Device-poll cadence is now configurable from a single CDK context flag, and
the default cadence moved from "manifest says 2 h, firmware clamps to 1 h"
to a coherent "both sides say 1 h."

### Changed
- **Default `next_check_after` hint dropped from 2 h to 1 h.** The firmware
  already capped sleep at 1 h via `SLEEP_MAX_SECONDS`, so devices were
  always polling hourly regardless of the 2-h hint. The manifest now tells
  the truth. Battery impact: zero (firmware behaviour unchanged at the
  default).
- **`compute_next_check_after`'s default `tick_interval` is now 1 hour.**
  Callers passing an explicit value are unaffected.

### Added
- **`EINKGEN_POLL_INTERVAL_SECONDS` env var** read by `publish.publish`.
  When set to a positive integer, overrides the manifest's
  `next_check_after` cadence. Unparseable / non-positive values silently
  fall back to the 1-hour default so a bad override can't take publish
  down.
- **CDK context flag `einkgenPollIntervalSeconds`.** Sets the env var
  above on both the generator and inbound-email Lambdas. Default unset →
  built-in 1 h. Validated at synth time (must be a positive integer);
  documented as something the operator must keep in lockstep with the
  firmware's `SLEEP_MAX_SECONDS`.
- **QUICKSTART §3.12** — optional "Device poll interval" section with a
  battery-life trade-off table (3 min → 3 weeks; 1 h → ~1 year; 3 h → ~2
  years on a 3000 mAh cell), the two edits needed (firmware constants +
  CDK context), and the clamp-asymmetry caveat.
- **Firmware comment block** above `SLEEP_*_SECONDS` documenting the
  cadence/battery trade-off and the required-in-lockstep relationship
  with `einkgenPollIntervalSeconds`.

## [0.3.2.0] - 2026-05-15

Firmware-only feature: the Inkplate now draws its own low-battery indicator on
the panel when charge runs low, so the device tells you it needs charging
without anyone having to look at the dashboard.

### Added
- **On-display low-battery overlay.** When `display.readBattery()` reports
  below `BATT_LOW_THRESHOLD_PCT` (default 10%), the firmware composites a
  small iPhone-status-bar-style battery icon with the percentage inside it
  into the top-right corner of the rendered frame (~80×32 px on the
  1200×825 panel — presence is enough, legible up close), on a white card so
  it stays readable over dark image regions. The image pipeline is
  unchanged — the badge only exists on the panel, never in the BMP that S3 /
  CloudFront serve, so the manifest's `image_sha256` doesn't churn and the
  cache stays hot. Implemented entirely in `firmware/inkplate10/inkplate10.ino`
  using Inkplate's `drawRect` / `fillRect` / `print` primitives between
  `drawBitmapFromBuffer()` and `display()`.

### Changed
- **Firmware redraw trigger.** The panel now also refreshes when reported
  battery crosses the low-battery threshold in either direction (tracked in
  NVS under `batt_low`), so the overlay can appear when charge drops and
  disappear when the device is plugged back in — previously the panel only
  redrew on `image_sha256` change. This adds at most two extra image
  downloads per battery cycle (one when crossing down, one when crossing
  back up); no extra OpenAI cost.

## [0.3.1.1] - 2026-05-15

Switch the image model from `gpt-image-1` to `gpt-image-2` and call it at
`quality="medium"` rather than the previous default. Both the text-to-image
and image-edit (restyle) paths are affected. Output size, the base prompt,
and the panel-side dither pipeline are unchanged — the visible difference is
cheaper per-call cost and the new model name in history manifests.

### Changed
- **OpenAI model.** `src/einkgen/core/generate.py` now calls `gpt-image-2` with
  `quality="medium"` for both `generate()` and `generate_from_image()`. The
  pipeline records `model: "gpt-image-2"` in `source` for generated and
  restyled frames; historical manifests keep their original `gpt-image-1`
  value. The 8-level e-paper dither erases sub-pixel detail anyway, so
  `quality="high"` was wasted spend.

## [0.3.1.0] - 2026-05-15

First hardware-validated firmware release. The Inkplate 10 boots, fetches
`current/manifest.json`, renders the BMP, posts status, and deep-sleeps.

### Fixed
- **Firmware: BMP-from-buffer render call.** The sketch was calling
  `display.drawImage(buf, x, y, len, dither, invert)`, a signature that
  doesn't exist on the Soldered `InkplateLibrary`. Replaced with
  `display.image.drawBitmapFromBuffer(buf, x, y, dither, invert)` — the
  call the library actually exposes on the Inkplate10 board driver, which
  reads width/height from the BMP header. Validated end-to-end on
  hardware: panel renders the queued frame and the Device tab populates
  within seconds. Without this, the sketch failed to compile against the
  installed library and could never have drawn.
- **Inbound email: rejection wording.** The non-allowlisted-sender reply
  now reads "is not authorised" instead of "isn't authorised", which also
  unbreaks the regression test that asserts the rejection message contains
  the phrase users actually search for.

### Changed
- **Quickstart: new Part 5 covers flashing the Inkplate.** Walks through
  toolchain setup, pulling the four `secrets.h` values from
  `cdk-outputs.json` + Secrets Manager, board / partition / upload-speed
  picks, and the two flash-time errors that surfaced on the first
  hardware-test pass (`No serial data received` → close Serial Monitor or
  hold the WAKE button; `Invalid head of packet` → lower upload speed to
  115200).
- **Firmware README.** Added a "Flash-time gotchas" section mirroring the
  QUICKSTART troubleshooting bullets, and marked `drawBitmapFromBuffer`
  as confirmed on hardware rather than a TODO for the bring-up pass.

## [0.3.0.1] - 2026-05-15

Live dashboard polish: the Queue tab now refreshes itself on a 10-second
cadence so new submissions appear without a manual reload, and the Device tab
re-fetches every time you click into it.

### Added
- **Queue auto-refresh.** The Queue tab polls `/queue` every 10 seconds and
  swaps the list in place — no flash of the "Loading queue…" placeholder on
  each tick. A small spinner in the header spins whenever a fetch is in
  flight and dims to idle between ticks; the label flips between
  "Refreshing…" and "Auto-refresh every 10s" so the state stays legible
  without motion. Transient poll errors are swallowed if a previous good
  list is on screen, so a single flaky request doesn't blank what the
  operator was reading.
- **Device tab refetch on click.** Clicking the Device tab — including
  re-clicking it while it's already active — forces the Device component to
  remount and fetch fresh status, instead of showing whatever loaded the
  first time the tab was opened.

### Changed
- **Email submissions can now combine subject and body as the prompt.** Previously
  the body was used only as a fallback when the subject was empty; now whenever
  both carry text they are concatenated (subject first, blank line, then the first
  meaningful body line) so a phone user can type a short subject ("watercolor")
  and elaborate in the body ("of a mountain at dawn"). Existing subject-only,
  body-only, and image + prompt restyle paths are unchanged.

## [0.3.0.0] - 2026-05-15

Email submission channel. The queue gains a new write path: send a prompt, an
image, or both to a configured email address. SMS is explicitly skipped — no
free AWS-native inbound option exists, and email covers the same share-sheet UX
on phones.

### Added
- **Inbound-email Lambda** (`einkgen-inbound-email`). SES receives mail at
  `*@<inboundDomain>`, drops the raw message into `s3://<bucket>/inbound/`, the
  Lambda parses MIME, checks the allowlist, stages any image attachment to
  `queue/staged/`, and calls `queue.enqueue(source="email")`. Replies with a
  queued-confirmation on success.
- **Sender allowlist** at `s3://<bucket>/config/email_allowlist.txt` — plain text,
  one address per line, `#` comments allowed. Managed by `einkgen allowlist
  {ls,add,rm}` or edited directly. Senders not on the list receive a friendly
  rejection email that does not name allowed addresses.
- **SES sender authentication.** Inbound messages are only trusted when
  `Authentication-Results` shows SPF or DKIM pass aligned with the From: domain.
  Unauthenticated messages are dropped silently (no reply, to avoid being a
  backscatter cannon for forged From: headers).
- **Image + prompt restyling.** `kind="image"` now accepts an optional prompt;
  if set, the upload is sent through `gpt-image-1`'s edit endpoint with the
  prompt as a restyle hint, otherwise it's a B&W passthrough as before. The
  CLI exposes this via `einkgen queue image <path> --prompt "<text>"`.
- **CDK construct `EinkgenInboundEmail`** gated behind a context flag
  (`einkgenInboundDomain`). The stack deploys clean without the flag; setting
  it provisions the SES EmailIdentity, receipt rule set, S3 trigger, Lambda,
  scoped IAM, **and the Route 53 DKIM CNAMEs + MX record** (when the hosted
  zone exists, which both setup paths in QUICKSTART §3.10.1 create). The
  only manual steps post-deploy are activating the receipt rule set (one
  active set per account; CDK doesn't clobber) and requesting SES production
  access for reply delivery.
- **Domain setup helper.** [infra/scripts/register-domain.example.sh](infra/scripts/register-domain.example.sh)
  is a Route 53 registration template (operator copies to
  `register-domain.sh` and fills ICANN-required contact info; the live
  copy is gitignored so PII never lands in the repo). QUICKSTART §3.11.1
  documents both this path and the alternative of delegating an existing
  externally-registered domain to Route 53. CLAUDE.md walks future agents
  through name research (`list-prices` filtered by sustainable renewal,
  then `check-domain-availability`).
- Docs: ARCHITECTURE §3 covers the email submission flow; QUICKSTART §3.10
  walks through the full setup; CLAUDE.md teaches agents how to handle the
  domain question; the SMS rationale is in ARCHITECTURE.

## [0.2.0.1] - 2026-05-15

Phase 3 — first real deploy. The system actually runs against AWS now; every bug
the deploy uncovered is fixed in this version.

### Changed
- Lambda Function URLs replaced with **API Gateway HTTP API** for `einkgen-read-api`
  and `einkgen-device-status`. AWS's account-level "block public access for Function URLs"
  rejects `AuthType: NONE`, so the URLs returned 403 from the auth layer. API Gateway
  public endpoints are not subject to that block. CORS rules preserved (read-api pinned
  to CloudFront + localhost; device-status has no CORS, firmware-only).
- Pillow now bundles into the generator Lambda zip directly. The Klayers public layer
  ARN baked into `infra/cdk.json` was no longer accessible to this account; bundling
  Pillow ourselves removes the third-party hosted-layer dependency.
- Lambda architecture flipped from `x86_64` → `arm64` (Graviton2). Native to Apple
  Silicon dev machines, avoids `--platform linux/amd64` bundling-quirks, ~20% cheaper.

### Fixed
- `infra/lib/observability.ts` — `AWS::Logs::MetricFilter` resources can't use
  `defaultValue + dimensions` together, and literal-token filter patterns can't
  populate dimensions at all. Switched to per-Lambda metric names
  (`ErrorLogCount-{generator,read-api,device-status}`) with `defaultValue: 0` restored.
  `cdk synth` accepted the original combination silently — only the CloudWatch
  API rejects it.
- `core/generate.py` — when `OPENAI_API_KEY` env var is unset, fetch the key from
  Secrets Manager using the `OPENAI_API_KEY_SECRET_NAME` env var the CDK already
  injects. Phase 1's `_default_client()` only read the env var, so the generator
  Lambda 500'd on every queue item until this landed. CLI path unchanged.
- `infra/.gitignore` and `web/.gitignore` — added `cdk-outputs.json` and
  `.env.production` / `.env.development`. Both are deployment state that varies
  per environment and shouldn't sit in source.

## [0.2.0.0] - 2026-05-14

Phase 2 — the system becomes end-to-end deployable. Web SPA, public read-only API,
device status ingestion, and the CDK stack that wires it all together.

### Added
- `einkgen-read-api` Lambda — public Function URL with `GET /queue`, `/history`, `/status`. Read-only IAM on the bucket. (Milestone 9)
- `einkgen-device-status` Lambda — `POST /` with `X-Device-Token` validated against Secrets Manager. Writes `status/device-<id>.json`. Module-scope token cache with 5-min TTL. (Milestone 11)
- Web app at `web/` — React + Vite + TypeScript SPA, three read-only tabs (Queue, History, Device), vanilla CSS, no UI library. (Milestone 10)
- AWS CDK infrastructure (TypeScript) at `infra/` — S3 bucket with Origin Access Control, CloudFront distribution (separate cache behaviors for `current/*`, `history/*`, `queue/staged/*`, and the SPA at `/web/`), three Lambdas, EventBridge `rate(2 hours)` cron, Secrets Manager, CloudWatch metric filters + dashboard. (Milestone 12 + README §9)
- `infra/scripts/check-errors.sh` — manual CloudWatch ERROR sweep across all three Lambdas.
- `VERSION`, `CHANGELOG.md`, `TODOS.md` — gstack conventions, previously a phase 1 loose thread.

### Security
- CloudFront `history/*` gated to `processed.bmp` only via a viewer-request function — raw `original.png` uploads no longer publicly readable through the CDN.
- Generator Lambda IAM narrowed from full-bucket `grantReadWrite` to explicit `current/*` + `history/*` + `queue/*` policy. Matches README §16 invariant.
- Device-status Function URL CORS removed entirely (was `*` wildcard); only firmware POSTs, no browser involvement.
- Device-status enforces a `device_id` regex, 4 KB body size cap, body-field allowlist, and `hmac.compare_digest` for token comparison.
- Token cache TTL bounds the window where a rotated secret is unreachable on warm Lambda containers.
- Synth-only Lambda asset stub now writes a `SYNTH_ONLY_DO_NOT_DEPLOY` sentinel; handlers refuse to import if present in `/var/task/`.

### Fixed
- Web SPA: `base: '/web/'` in Vite config so the built `index.html` references the correct asset paths (without it the app would not boot in production).
- `cdnUrl()` now `encodeURI`s the path so staged filenames with spaces/`#`/`?` don't break image rendering.
- `/history` no longer reads every manifest in the bucket; it lex-sorts ULID-keyed manifest keys descending and reads only the top N. Empty `generated_at` entries are dropped so a single malformed manifest can't poison the listing.
- `/queue` now honors `?limit=` (default 200, max 1000) — bounds work when the generator wedges and the queue grows.
- `/status` `last_modified` uses the `Z` suffix to match `last_seen` elsewhere.
- Generator Lambda async retries set to `0` on both the function and the EventBridge target — was up to 3× OpenAI spend per transient failure.
- CloudFront serves `web/index.html` with `max-age=0, must-revalidate` and `web/assets/*` with `max-age=31536000, immutable` — prevents stale-shell / deleted-asset-hash issues on redeploys.
- CloudWatch `MetricFilter` now uses `fn.logGroup` instead of `fromLogGroupName` — eliminates deploy-time race against the `LogRetention` custom resource.
- Bare CloudFront domain (`/`) now rewrites to `/web/index.html` instead of returning S3 NoSuchKey.

## [0.1.0] - 2026-05-13

Phase 1 — image pipeline + queue + generator Lambda + firmware.

### Added
- CLI: `einkgen local generate|convert|preview`, `einkgen queue prompt|image|ls|rm`, `einkgen status`, `einkgen history`.
- Image pipeline: OpenAI `gpt-image-1` at 1536×1024 → center-crop to 1200×825 → grayscale → Atkinson/Floyd–Steinberg dither → 8-bit indexed BMP.
- S3-prefix queue at `queue/<iso8601>-<ulid>.json` with monotonic-FIFO semantics.
- Generator Lambda — triggered by S3 `ObjectCreated` on `queue/` and EventBridge `rate(2h)`. Reserved concurrency = 1 for serial drain.
- Publish primitive — writes `current/manifest.json` + `current/image.bmp`, archives to `history/<id>/`, invalidates CloudFront.
- Inkplate 10 firmware — fetches manifest, downloads + SHA-256-verifies image, redraws if hash changed, POSTs status, deep-sleeps until `min(next_check_after, now + 1h)`.
- 68 pytest cases against moto-backed S3.
