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

### Profile and replace pure-Python error-diffusion dither
**Priority:** P2
**Source:** Phase 1 pre-landing review

`core/convert.py` does Atkinson and Floyd–Steinberg in pure Python at 1200×825.
Likely slow inside Lambda (still untimed in production). Once we have a real
measurement, swap to numpy or `Pillow.Image.convert("P", dither=Image.FLOYDSTEINBERG)`.
Don't preemptively rewrite.

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

### Daily OpenAI cost cap
**Priority:** P3
**Source:** README §15 + phase 2 adversarial review

No daily $ cap on OpenAI spend. `retryAttempts: 0` on the generator Lambda + the
EventBridge target caps retry-amplification. Cost-runaway from a high-volume
legitimate queue is bounded only by reserved concurrency = 1 + the 2h cron
interval. Add a CloudWatch alarm on the generator's invocation count + OpenAI
usage when convenient.

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

(none yet — this file was bootstrapped in v0.2.0.0)
