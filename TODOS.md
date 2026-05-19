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

### Concurrent `/wake` calls race on `current/manifest.json` version increment
**Priority:** P3
**Source:** 0.6.0.0 security review (race condition)

`set_current_from_history` is now called from `/wake` (concurrency = 5) plus
admin `/admin/show` plus the generator's `publish_item` path. Two concurrent
calls both read `previous_version = N`, both write `N+1` — classic lost-update
on the manifest's `version` field. Not a correctness issue for the device (it
keys off `image_sha256`, not `version`) but the monotonicity property
documented in ARCHITECTURE §7 doesn't hold.

Fix: switch `current/manifest.json` writes to use S3 `If-Match`/`If-None-Match`
conditional puts and retry on collision, OR funnel all current-manifest writes
through the generator Lambda (concurrency = 1).

### Empty-prompt-library deadlock leaves buffer drained with no signal
**Priority:** P3
**Source:** 0.6.0.0 adversarial review

If the operator clears the prompt library from the Admin tab AND `expand_topic`
fails (text-LLM down, OpenAI outage) so the raw-topic fallback also can't enqueue
anything, the cron buffer-refill loop exits without rendering. The buffer drains
over a few `/wake` calls and stays empty; `/wake` returns `queue_empty` forever
with no operator-visible alert. Pre-0.6.0.0 the equivalent was "cron didn't
render", which had the same outcome but was less prominent because there was no
buffer-depth concept.

Fix: a CloudWatch alarm on generated-queue depth = 0 for >2 cron ticks. Or have
the SPA Admin tab surface "buffer empty, library may need topics".

## Read API

### Multi-device `/devices` endpoint
**Priority:** P3
**Source:** Phase 2 adversarial review

`/status` returns the single newest device by S3 `LastModified`. Once multi-device
deployments exist, add a `/devices` listing endpoint so the SPA Device tab can
choose between them. Today this is a no-op because all devices alias to `default`.

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

### CloudFront invalidation `CallerReference` collisions
**Priority:** P4
**Source:** Phase 2 adversarial review

`core/publish.py` uses `datetime.now(...).timestamp()` as the `CallerReference`
for `cloudfront:CreateInvalidation`. Two near-simultaneous publishes (already
unlikely with reserved concurrency = 1) would collide. Switch to the item id or
a UUID; treat any invalidation failure as a non-fatal warning rather than letting
it surface as a Lambda retry.

### Strip operator filename from staged keys
**Priority:** P4
**Source:** Phase 2 adversarial review

`cli/queue.py` writes `queue/staged/<sha8>-<original-filename>.<ext>`. The
filename is preserved verbatim — operator-controlled, but minor PII leakage
through the public CDN behavior on `queue/staged/*`. Use `queue/staged/<sha8>.<ext>`
instead.

## Completed

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
