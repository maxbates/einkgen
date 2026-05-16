# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses a 4-digit version scheme (MAJOR.MINOR.PATCH.MICRO).

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
