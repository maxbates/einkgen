# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses a 4-digit version scheme (MAJOR.MINOR.PATCH.MICRO).

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
