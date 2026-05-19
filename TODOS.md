# TODOS

Tracked follow-ups for einkgen. Grouped by component, then priority (P0 → P4),
then by completion. Format per `~/.claude/skills/review/TODOS-format.md`.

## Firmware

### Cert-pin CloudFront against MITM
**Priority:** P1
**Source:** Phase 1 pre-landing review (deferred to phase 2.x)

Firmware currently calls `client.setInsecure()` before HTTPS GET for the manifest +
image fetch and the status POST. The SHA-256 verify path neutralises the worst
MITM impact (an attacker can deny service but can't inject content the device will
draw), but real cert pinning against the CloudFront chain is the v1 fix.

### Send `device_id` in status POST
**Priority:** P2
**Source:** Phase 2 device-status Lambda design

`firmware/inkplate10/inkplate10.ino::postStatus` sends `{battery_v, battery_pct,
rssi, current_hash, fw_version}` — no `device_id`. The Lambda falls back to
`"default"`, which collapses every device onto `status/device-default.json`.
Once we deploy more than one Inkplate, derive a stable id (ESP32 MAC, or a value
baked into `secrets.h`) and include it.

## Image pipeline

### ~~Profile and replace pure-Python error-diffusion dither~~ (measured, closed)
**Priority:** P2 → resolved (no action)
**Source:** Phase 1 pre-landing review
**Measured:** Phase 3 post-deploy (2026-05-15)

Profiled against `history/01KRPJPKZZ0MJ5RRZYXEHRWBWV/original.png` (a real
1536×1024 OpenAI output) locally and cross-referenced with 7 successful
CloudWatch invocations on Lambda ARM64 Graviton2 1024 MB.

| Phase | Time | % of total |
| --- | --- | --- |
| OpenAI `gpt-image-1` call | ~52–55 s | ~93–95% |
| `core/convert.py` (load + crop + grayscale + **Atkinson dither** + BMP encode) | ~2–3 s | ~4–5% |
| `core/publish.py` (S3 puts + CloudFront invalidate) | ~0.5–1 s | ~1–2% |
| **Total CloudWatch `Duration`** | **48.5–61.8 s** (n=7, mean ~55 s) | 100% |

Local breakdown of `convert()` on an M-series Mac (warm, n=3):
- PIL load: 29 ms
- `_fit_to_canvas` (center crop, no resampling): 0.3 ms
- `_to_grayscale`: 0.5 ms
- **`_dither_error_diffuse` (Atkinson, pure Python)**: **1106 ms**
- `_encode_indexed_bmp`: 41 ms
- Total `convert()`: **1161 ms**

For comparison: Pillow's native `Image.quantize(..., dither=FLOYDSTEINBERG)`
against the same 8-grey palette ran in **~11 ms** (≈100× faster), but only
supports Floyd–Steinberg — there is no built-in Atkinson and Atkinson is the
intentional default for the "crisp Mac look" the project wants (ARCHITECTURE §6).
A naive numpy port of the same algorithm ran *slower* (~5.5 s) because per-pixel
numpy ops are dominated by Python overhead — vectorisation isn't trivially
available for serial error diffusion.

**Decision: leave the pure-Python Atkinson in place.** Dither is 4–5% of total
runtime; OpenAI is the only thing worth optimising and we can't (it's a network
call). Revisit only if we drop OpenAI for a local model.

## Generated queue / /wake

### Cron-down silent failure mode not alarmed
**Priority:** P3
**Source:** 0.6.5.0 adversarial review

The `BUFFER_EMPTY_AFTER_REFILL` alarm shipped in 0.6.5.0 covers "cron
ran but couldn't refill". It does NOT cover "cron didn't run at all"
(EventBridge wedged, Lambda permission issue, Lambda timing out before
the marker emit). `treatMissingData: NOT_BREACHING` deliberately
silences missing datapoints to avoid false alarms during fresh-deploy
quiescence, but that same setting also silences a fully-broken cron.

Fix: a second CloudWatch alarm on `AWS/Lambda Invocations` for
`einkgen-generator` over a rolling 2 h window with threshold `< 1`
publishes to the same SNS topic. Document under CLAUDE.md "It's broken
/ debug this" when it lands.

## Read API

### Unauthenticated `/devices` leaks fleet inventory on multi-device deploys
**Priority:** P3
**Source:** 0.6.5.0 adversarial review

Today the single-device default deploys are a no-op (all reports
alias to `default`). The moment a second device ships with a real
`device_id` baked into `secrets.h` (see the firmware `device_id`
TODO above), `GET /devices` will return battery, RSSI, `fw_version`,
and `current_hash` for every device to anyone with the public CDN
URL. RSSI patterns are a weak geolocation hint; `fw_version` is a
CVE-targeting hint.

Fix options: (a) gate `/devices` behind the admin cookie (re-uses the
same auth as `/admin/*`); (b) redact the device_id to a hash on the
response and drop `current_hash` from the public shape; (c) keep
`/devices` public but require an opt-in `?include_sensitive=true`
that the admin client supplies. Cap and DoS bound are already in
place via `MAX_DEVICES_LIMIT`.

## Image pipeline

### `queue/staged/<sha8>.<ext>` allows collision-overwrite of submitter images
**Priority:** P3
**Source:** 0.6.5.0 adversarial review

The 8-char SHA-256 prefix is 32 bits. An attacker on the email
allowlist (or holding the admin cookie) can grind a colliding image
in milliseconds and overwrite another submitter's staged image at the
same CDN-visible key. The queue item still points at the same
`queue/staged/<sha8>.jpg`, so the rendered frame becomes
attacker-controlled. Pre-0.6.5.0 the same collision space existed
(`<sha8>-<filename>`); 0.6.5.0 didn't make it worse, but it didn't
fix it either.

Fix: widen the prefix to `sha256[:32]` (128 bits → birthday bound
~1.8 × 10^19) or append a ULID disambiguator (`<sha8>-<ulid>`).
Either approach is a two-line change in `core.queue.build_staged_key`
but breaks in-flight `queue/staged/*` keys, so deploy with the
prompt-queue drained.

## Infrastructure

### Install Codex CLI for full adversarial review
**Priority:** P3
**Source:** Phase 2 /review

`codex` was unavailable when phase 2 shipped, so the adversarial pass ran 2 of 4
tiers (Claude structured + Claude adversarial subagent). Run `npm install -g
@openai/codex` to enable the Codex structured review and Codex adversarial
challenge on future releases.

### Daily OpenAI cost circuit-breaker (Option B)
**Priority:** P3 *(was P2; Option A — CloudWatch alarm + SNS topic on the
generator's 24 h invocation count — shipped in [0.6.2.0])*
**Source:** PLAN §3 + phase 2 adversarial review + 0.6.0.0 security review

Option A (observability) is live: a CloudWatch alarm on
`AWS/Lambda Invocations` for `einkgen-generator` over a 24 h window
publishes to an SNS topic. Threshold = `einkgenDailyRenderCap` (default
100/day ≈ ~$4/day at `gpt-image-2` medium). Subscribe an inbox via
`einkgenAlarmEmail` — see QUICKSTART §3.13. When it fires the operator
gets paged and manually disables the cron rule.

Option B (auto-stop): a small Lambda subscribed to the same SNS topic
that calls `events:DisableRule` on `einkgen-generator-cron` when the
alarm transitions to ALARM. Operator manually re-enables once they've
investigated. Document the unblock procedure under CLAUDE.md "It's
broken / debug this" when this lands.

Only worth doing if Option A alarms aren't enough — i.e. if the operator
finds themselves repeatedly waking to drained budget and wishes the
system had stopped itself. Otherwise paging is sufficient.

## Completed

### ~~Concurrent `/wake` calls race on `current/manifest.json` version increment~~ (resolved in 0.6.5.0)
**Priority:** P3 → resolved
**Source:** 0.6.0.0 security review

`current/manifest.json` writes now go through a compare-and-swap helper
(`_write_current_manifest_cas` in `core/publish.py`) using S3 `If-Match` /
`If-None-Match` conditional puts with a bounded retry loop. Two concurrent
`/wake` advances both reading version=N can no longer both write N+1; the
loser re-reads and bumps to N+2.

### ~~Empty-prompt-library deadlock leaves buffer drained with no signal~~ (resolved in 0.6.5.0)
**Priority:** P3 → resolved
**Source:** 0.6.0.0 adversarial review

The generator now logs a literal `BUFFER_EMPTY_AFTER_REFILL` token at the
end of any cron tick that finishes with generated-queue depth = 0. A CDK
metric filter + alarm (`einkgen-<env>-generated-queue-empty`) pages the
operator via the existing alarm SNS topic after two consecutive empty
ticks. No SPA banner — the page is enough.

### ~~Multi-device `/devices` endpoint~~ (resolved in 0.6.5.0)
**Priority:** P3 → resolved
**Source:** Phase 2 adversarial review

`GET /devices` on the read-api returns every `status/device-<id>.json`
record newest-first. The typed client lives in `web/src/api.ts` as
`getDevices()` so a future multi-device deployment can list them
without a Lambda change. Still a no-op for the single-device default
deploy (every report aliases to `default`).

### ~~CloudFront invalidation `CallerReference` collisions~~ (resolved in 0.6.5.0)
**Priority:** P4 → resolved
**Source:** Phase 2 adversarial review

`core/publish.py::_invalidate_cloudfront` now uses `uuid.uuid4()` for
`CallerReference` and treats `ClientError` as a logged warning rather than
letting it propagate as a Lambda retry.

### ~~Strip operator filename from staged keys~~ (resolved in 0.6.5.0)
**Priority:** P4 → resolved
**Source:** Phase 2 adversarial review

All three staging callers (`cli/queue.py`, `admin_api.py`,
`inbound_email.py`) now go through `core/queue.build_staged_key`, which
produces `queue/staged/<sha8><ext>` with the extension constrained to a
small image-type allowlist. The CDN URL never exposes the
operator-supplied filename.

### ~~Device-status CORS header advertises `*` despite firmware-only intent~~ (resolved in 0.6.5.0)
**Priority:** PLAN §4 open question → resolved
**Source:** Phase 2 adversarial review

`Access-Control-Allow-Origin` was being set defensively to `*` on every
device-status response. The endpoint is firmware-only (no browser caller)
and the HTTP API doesn't configure CORS, so the header was misleading.
Dropped from `_RESPONSE_HEADERS`; the SPA never called this Lambda.

### ~~Embed manifest fields in `/wake` response to skip the stale CloudFront fetch~~ (resolved in 0.6.1.0)
**Priority:** P1 → resolved
**Source:** 0.6.0.0 adversarial review

After a `/wake` advance the device immediately `GET`-ed `current/manifest.json`,
but CloudFront caches that path with `defaultTtl: 60s`/`maxTtl: 300s` and the
in-flight `CreateInvalidation` typically takes 5–60 s to propagate, so the fetch
returned the pre-advance manifest and the device skipped the redraw until the
next wake cycle.

Fixed in 0.6.1.0: the `/wake` 200 response now includes `image_url`,
`image_sha256`, `image_bytes`, and `next_check_after` for both `action=advance`
and `action=redraw`. Firmware feeds them straight into `downloadVerifyAndDraw`
and skips the follow-up GET. `action=queue_empty` and a server rollback both
degrade cleanly to the legacy `fetchManifest` path.

### ~~Submissions via email / CLI shouldn't disappear behind the buffer~~ (resolved in 0.6.3.0)
**Priority:** P2 → resolved
**Source:** 0.6.0.0 adversarial review (UX regression)

Pre-0.6.0.0, an inbound email or `einkgen queue prompt` submission rendered on
the next cron tick (≤ 30 min). After 0.6.0.0 those submissions landed on the
prompt queue and only got buffered into `generated/` after cron drained 10
items ahead of them — user-visible latency was 10 × 30 min = ~5 h.

Fixed in 0.6.3.0 (option (a) from the original fix sketch). Both submission
paths now grow an explicit "render this now" affordance that enqueues at the
top of the queue and async-invokes the generator with `render_now`, the same
as the SPA Admin tab's **Now** button:

- CLI: `einkgen queue prompt "<text>" --now` (and `einkgen queue image <path>
  --now`). Mutually exclusive with `--top`. Requires `lambda:InvokeFunction`
  on `einkgen-generator` in the operator's IAM.
- Email: subject prefix `NOW `, `NOW:`, or `[NOW]` (case-insensitive). The
  trigger is stripped from the prompt before generation. CDK wires
  `generator.grantInvoke` on the inbound-email Lambda and sets
  `EINKGEN_GENERATOR_FUNCTION_NAME` in its env.

Option (b) — preferentially draining non-`source="cron"` items first — was
rejected because the failure mode is worse: a flurry of email submissions
would push every cron-topup behind them, and the affordance is implicit
("did my email render fast?") instead of operator-driven.
