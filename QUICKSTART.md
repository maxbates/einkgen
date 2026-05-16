# Quickstart

Get einkgen running in your own AWS account. The first half (Part 1) is the
short list of things only a human can do — accounts, tooling, secrets. The
second half (Parts 2–4) is a runbook designed for an AI coding agent to
execute, but a human can follow it just as well.

For *what* you're deploying, see [ARCHITECTURE.md](ARCHITECTURE.md).
If you're an agent reading this, also read [CLAUDE.md](CLAUDE.md) for
the hard rules (cost, secrets, destructive-op gates).

---

## Part 1 — Human prerequisites (do these first)

Front-loaded so you can do them once, walk away, and hand the rest to an
agent.

### 1.1. Accounts

- **AWS account** with admin (or at least: IAM, S3, CloudFront, Lambda,
  API Gateway, Secrets Manager, EventBridge, CloudWatch). Note the
  12-digit account ID — you'll paste it once.
- **OpenAI API key.** Create one at
  <https://platform.openai.com/api-keys>. Copy the `sk-...` value to your
  password manager. The agent will paste it into Secrets Manager on your
  behalf; it never lives in source.

### 1.2. Local tooling

| Tool | Why | Check |
| --- | --- | --- |
| Python 3.11+ | CLI + Lambda runtime parity | `python3 --version` |
| Node.js 20+ | CDK + Vite | `node --version` |
| AWS CLI v2 | profile config, secret writes | `aws --version` |
| Docker (running) | CDK bundles Lambdas in a Python container | `docker info` |

On macOS: `brew install python node awscli` and install Docker Desktop.
On Linux: distro packages or `nvm`/`pyenv` for version pinning.

### 1.3. Clone

```sh
git clone https://github.com/<your-fork>/einkgen.git
cd einkgen
```

### 1.4. Configure an AWS profile

The project defaults to a profile named `einkgen` and the `us-east-1`
region. Pick whatever you like — the agent runbook will respect
`AWS_PROFILE` and `AWS_REGION`.

```sh
aws configure --profile einkgen
# AWS Access Key ID:     <yours>
# AWS Secret Access Key: <yours>
# Default region name:   us-east-1
# Default output format: json

# Sanity check — should print your account ID:
AWS_PROFILE=einkgen aws sts get-caller-identity
```

### 1.5. Pick a device-status token

This is the shared secret the Inkplate firmware will send in every status
POST. Generate one now; the agent will store it in Secrets Manager and you
will later bake the same value into
`firmware/inkplate10/secrets.h` when the device arrives.

```sh
openssl rand -hex 32
```

Save the value somewhere private. Don't paste it into chat or commit it
anywhere.

### 1.6. (One-time) CDK bootstrap

If you've never used CDK in this account+region, you'll need to bootstrap
the CDK toolkit stack once. The agent can do it for you in step 3.3, or
you can run it now:

```sh
cd infra && npm install
AWS_PROFILE=einkgen AWS_REGION=us-east-1 npx cdk bootstrap
cd ..
```

That's it for human steps. Hand the rest to an agent.

---

## Part 2 — Hand-off prompt for an agent

Paste something like the following into Claude Code (or whichever agent
you use) inside the repo root:

> Deploy einkgen to my AWS account by following
> [QUICKSTART.md](QUICKSTART.md) Part 3 step by step. I have already done
> Part 1. Settings:
>
> - `AWS_PROFILE=einkgen`
> - `AWS_REGION=us-east-1`
> - Environment name: `dev`
> - OpenAI API key: (I'll paste when you ask)
> - Device-status token: (I'll paste when you ask)
>
> Run each command, summarise the output, and only proceed if the previous
> step succeeded. Don't print secret values back at me — show length or a
> SHA-256 prefix instead. Stop and ask if anything looks unexpected.

The agent needs permission to run `aws`, `npx cdk`, `npm`, and read/write
files in the repo. Standing approval for `cdk deploy` to *your* account
makes the runbook smoother.

---

## Part 3 — Agent deploy runbook

All commands assume `AWS_PROFILE=einkgen AWS_REGION=us-east-1` (override
via env vars). All paths are relative to the repo root.

### 3.1. Sanity-check the environment

```sh
AWS_PROFILE=einkgen AWS_REGION=us-east-1 aws sts get-caller-identity
node --version       # >= 20
python3 --version    # >= 3.11
docker info >/dev/null && echo "docker ok"
```

If `docker info` fails, ask the human to start Docker Desktop. CDK Lambda
bundling needs a running daemon.

### 3.2. Install JS dependencies

```sh
( cd infra && npm install )
( cd web   && npm install )
```

### 3.3. Bootstrap CDK (idempotent)

Safe to run even if already bootstrapped:

```sh
( cd infra && AWS_PROFILE=einkgen AWS_REGION=us-east-1 npx cdk bootstrap )
```

### 3.4. First deploy — infrastructure only (no web assets yet)

The web SPA needs the deployed API/CDN URLs at build time, so the first
deploy goes out without `web/`. `includeWebAssets` defaults to `false`.

```sh
( cd infra && AWS_PROFILE=einkgen AWS_REGION=us-east-1 npx cdk deploy \
    --outputs-file cdk-outputs.json \
    --require-approval never \
    -c env=dev )
```

After it finishes, `infra/cdk-outputs.json` contains the URLs you need:

```sh
jq '.["EinkgenStack-dev"]' infra/cdk-outputs.json
```

Expected keys: `BucketName`, `CdnDomain`, `CdnDistributionId`,
`ReadApiUrl`, `DeviceStatusUrl`, `OpenAiSecretName`,
`DeviceStatusSecretName`.

### 3.5. Populate the secrets

The stack created both secrets with placeholder strings
(`REPLACE_ME_POST_DEPLOY`). Overwrite them with the values the human gave
you:

```sh
AWS_PROFILE=einkgen AWS_REGION=us-east-1 aws secretsmanager put-secret-value \
  --secret-id einkgen/openai_api_key \
  --secret-string "<OPENAI_API_KEY>"

AWS_PROFILE=einkgen AWS_REGION=us-east-1 aws secretsmanager put-secret-value \
  --secret-id einkgen/device_status_token \
  --secret-string "<DEVICE_STATUS_TOKEN>"
```

Verify *length only* so you don't echo secrets:

```sh
AWS_PROFILE=einkgen AWS_REGION=us-east-1 aws secretsmanager get-secret-value \
  --secret-id einkgen/openai_api_key --query SecretString --output text \
  | wc -c
# expect: ~50 chars for an OpenAI key

AWS_PROFILE=einkgen AWS_REGION=us-east-1 aws secretsmanager get-secret-value \
  --secret-id einkgen/device_status_token --query SecretString --output text \
  | wc -c
# expect: 64 chars for a hex32 token + 1 newline = 65
```

### 3.6. Build the web SPA against the deployed URLs

Read the URLs from `infra/cdk-outputs.json` and write `web/.env.production`
(gitignored):

```sh
READ_API_URL=$(jq -r '.["EinkgenStack-dev"].ReadApiUrl'  infra/cdk-outputs.json)
CDN_DOMAIN=$(jq -r '.["EinkgenStack-dev"].CdnDomain'     infra/cdk-outputs.json)

cat > web/.env.production <<EOF
VITE_READ_API_URL=${READ_API_URL}
VITE_CDN_BASE=https://${CDN_DOMAIN}
EOF

# If you enabled inbound email in §3.11, add the domain so the Queue tab
# shows the "email anything @<domain>" submission hint. Match this to the
# `einkgenInboundDomain` flag you deploy with.
# echo "VITE_INBOUND_EMAIL_DOMAIN=einkgen.link" >> web/.env.production

( cd web && npm run build )
```

This emits `web/dist/`.

### 3.7. Redeploy with the web assets

```sh
( cd infra && AWS_PROFILE=einkgen AWS_REGION=us-east-1 npx cdk deploy \
    --outputs-file cdk-outputs.json \
    --require-approval never \
    -c env=dev -c includeWebAssets=true )
```

This uploads `web/dist/*` to `s3://einkgen-dev/web/` and invalidates
`/web/*` in CloudFront.

### 3.8. Smoke test

```sh
READ_API_URL=$(jq -r '.["EinkgenStack-dev"].ReadApiUrl'      infra/cdk-outputs.json)
DEVICE_URL=$(jq   -r '.["EinkgenStack-dev"].DeviceStatusUrl' infra/cdk-outputs.json)
CDN_DOMAIN=$(jq   -r '.["EinkgenStack-dev"].CdnDomain'       infra/cdk-outputs.json)

# Read API should respond 200 with an empty queue.
curl -sS "${READ_API_URL}/queue"

# Device-status should reject unauthenticated POSTs.
curl -sS -o /dev/null -w "no token   -> %{http_code}\n" \
  -X POST "${DEVICE_URL}/" -H 'Content-Type: application/json' -d '{}'

# CloudFront should serve the SPA index.html.
curl -sS -I "https://${CDN_DOMAIN}/" | head -3
```

Open `https://<CdnDomain>/` in a browser. You should see the three-tab
SPA with `Queue is empty.`, `Device has not reported yet.`, and an empty
History grid. Within 2 hours the EventBridge cron will tick and you'll
see your first frame appear.

### 3.9. (Optional) Enqueue a test prompt now

If the human wants a frame immediately without waiting for cron, install
the Python package and enqueue one:

```sh
pip install -e .

EINKGEN_BUCKET=einkgen-dev \
AWS_PROFILE=einkgen AWS_REGION=us-east-1 \
einkgen queue prompt "Geometric composition. Overlapping circles, squares, triangles in bold flat shapes with high contrast."
```

The generator Lambda will pick it up within a few seconds. Tail logs to
watch the ~55 s run:

```sh
AWS_PROFILE=einkgen AWS_REGION=us-east-1 \
  aws logs tail /aws/lambda/einkgen-generator --follow
```

When the run completes, refresh the SPA's History tab. The new tile
appears at the top.

### 3.10. (Optional) Custom domain for the site

By default the SPA is served from `https://<id>.cloudfront.net/`. If you
own (or want to register) a domain — same one you'd use for inbound email
in §3.11 — CDK can host the SPA at `https://<yourdomain>/` instead. Same
two paths as §3.11.1 to get a Route 53 hosted zone (register a new
domain, or delegate an existing one), then deploy with:

```sh
( cd infra && AWS_PROFILE=einkgen AWS_REGION=us-east-1 npx cdk deploy \
    --outputs-file cdk-outputs.json \
    --require-approval never \
    -c env=dev -c includeWebAssets=true \
    -c einkgenSiteDomain=<yourdomain> )
```

The deploy:

- Issues an ACM certificate for `<yourdomain>` with a `*.<yourdomain>`
  SAN (DNS-validated against the Route 53 zone you control — no manual
  CNAME copy/paste).
- Adds `<yourdomain>` as an alternate domain name on the CloudFront
  distribution.
- Creates apex A + AAAA alias records pointing at the distribution
  (apex CNAME isn't valid per RFC 1034; Route 53's alias is the AWS
  primitive that resolves to CloudFront's IPs at query time).
- Switches the manifest `image_url` baked into future generations to
  use the custom domain (purely cosmetic — device firmware fetches from
  whatever URL it was flashed with).

ACM cert validation + CloudFront distribution propagation: 5–30 min.
`cdk deploy` will block on cert validation, then return; CloudFront's
own propagation continues in the background but `https://<yourdomain>/`
typically resolves within a few minutes of deploy completion.

If you want both the site **and** inbound email on the same domain
(the simplest setup), set both flags together — CDK reuses the same
hosted zone:

```sh
... -c einkgenSiteDomain=einkgen.link \
    -c einkgenInboundDomain=einkgen.link
```

The two flags can't currently point at *different* domains in the same
stack (assertion in [infra/lib/einkgen-stack.ts](infra/lib/einkgen-stack.ts)
will fail synth) — split that out when you actually need it.

### 3.11. (Optional) Email submission channel

The base stack is read-only-public; submitting requires the operator's
laptop CLI. To accept submissions via email instead — text, an image, or
both — turn on the inbound-email path. It costs ~$5/year for a domain
plus per-email fractions of a cent.

This step is **opt-in**. Skip it if email submission isn't a goal.

#### 3.11.1. Pick or attach a domain

SES inbound needs a domain you control DNS for. `*.cloudfront.net` is
AWS-owned and not usable. Two paths:

**Path A — register a new domain via Route 53.** Cheapest sustainable
option is a `.link` ($5/yr) or `.click` ($3/yr). Use the helper script:

```sh
# Copy the template (live script is gitignored — it holds your address + phone)
cp infra/scripts/register-domain.example.sh infra/scripts/register-domain.sh

# Edit DOMAIN= at the top and the eight REPLACE_WITH_* lines (first/last
# name, street, city, state, zip, phone, email) — ICANN requires real
# registrant contact info; WHOIS-masked by default.
$EDITOR infra/scripts/register-domain.sh

# Run it
./infra/scripts/register-domain.sh
```

The script:

1. Confirms the name is available (`route53domains check-domain-availability`).
2. Submits `route53domains register-domain` with privacy protection on
   all three contacts (admin / registrant / tech).
3. Prints the operation ID for status tracking.

Track completion (5–30 min typical):

```sh
aws route53domains get-operation-detail --operation-id <op-id> \
  --region us-east-1 --profile einkgen
```

When status is `SUCCESSFUL`, AWS auto-creates a Route 53 hosted zone for
the domain. Continue to §3.11.2.

For an **agent** doing the picking: enumerate cheap+sustainable TLDs with
`aws route53domains list-prices --region us-east-1 --output json`
(filter where `RegistrationPrice.Price` ≤ `$10` *and*
`RenewalPrice.Price` ≤ `$10` so first-year promos don't trick you).
Check availability for candidate names with
`aws route53domains check-domain-availability --domain-name <name>`.
Surface the top 3–5 picks with their renewal price *before* committing
to a registration — domain registration is a recurring cost decision.

**Path B — attach an existing domain you already own.** If you have a
domain registered at Namecheap, GoDaddy, Cloudflare, etc., you don't
need to transfer it. You just delegate its DNS to Route 53:

1. Create a hosted zone in Route 53:
   ```sh
   AWS_PROFILE=einkgen aws route53 create-hosted-zone \
     --name <yourdomain.com> \
     --caller-reference "einkgen-$(date +%s)" \
     --region us-east-1
   ```
2. The response includes a `NameServers` list (four `ns-*.awsdns-*` names).
3. At your registrar's DNS panel, replace the existing name-server records
   with those four. Propagation: ~5–30 min.
4. Verify resolution:
   ```sh
   dig +short NS <yourdomain.com>
   ```
   Should return all four AWS name servers. Once it does, continue.

You can use the apex (e.g. `yourdomain.com`) or a subdomain
(e.g. `submit.yourdomain.com`). Subdomain is recommended if the apex
already receives mail — Route 53 will own MX for whatever zone you
configure, and SES inbound conflicts with any pre-existing mail server
for the same name.

If you go subdomain: create the subdomain's hosted zone (same command,
with `--name submit.yourdomain.com`), then at the parent zone (wherever
that lives) add NS records for `submit` pointing to the new zone's
nameservers. Skip the registrar step.

#### 3.11.2. Deploy SES inbound with CDK

CDK reads the domain from a context flag and wires up the SES
EmailIdentity, receipt rule set, S3 trigger, and Lambda only when set:

```sh
( cd infra && AWS_PROFILE=einkgen AWS_REGION=us-east-1 npx cdk deploy \
    --outputs-file cdk-outputs.json \
    --require-approval never \
    -c env=dev -c includeWebAssets=true \
    -c einkgenInboundDomain=<yourdomain> \
    -c einkgenProjectUrl=https://github.com/<you>/einkgen )
```

The deploy:

- Looks up the Route 53 hosted zone for the domain (created in §3.11.1).
  If the zone doesn't exist yet, synth fails fast with a clear error.
- Creates the SES `EmailIdentity` and publishes the three DKIM CNAMEs
  into the zone automatically.
- Creates the MX record pointing at `inbound-smtp.<region>.amazonaws.com`.
- Creates the `einkgen-inbound` receipt rule set with one catch-all rule.
- Wires the inbound Lambda to S3 ObjectCreated on `inbound/*`.
- Optionally seeds `config/email_allowlist.txt` with the addresses passed
  via the `einkgenAllowlistSeed` context flag (comma-separated). The seed
  is **never** hardcoded in committed CDK code — pass it on the CLI so the
  list doesn't leak into the repo:
  ```sh
  ... -c einkgenAllowlistSeed=you@gmail.com,partner@gmail.com
  ```
  The seed runs **once** per construct creation; subsequent `cdk deploy`
  runs don't touch the file, so `einkgen allowlist add/rm` edits are
  preserved. If you skip the flag, the allowlist starts empty and you
  manage it entirely via the CLI.

After the deploy, two finishing steps remain (CDK can't do them):

1. **Activate the receipt rule set.** Only one rule set per account can
   be active at a time, so CDK doesn't flip it for you:
   ```sh
   AWS_PROFILE=einkgen aws ses set-active-receipt-rule-set \
     --rule-set-name einkgen-inbound --region us-east-1
   ```
   Verify:
   ```sh
   AWS_PROFILE=einkgen aws ses describe-active-receipt-rule-set --region us-east-1
   ```

2. **Request SES production access.** Without it, the account is in
   sandbox and SES will refuse to send confirmation/rejection replies to
   any address not pre-verified. Submit the form at
   [SES Console → Account Dashboard → Request production access](https://us-east-1.console.aws.amazon.com/ses/home?region=us-east-1#/account).
   Approval is typically <24h. Until then, *inbound enqueue still works*
   — only the auto-replies are blocked.

#### 3.11.3. Test the inbound flow

Send a test email from an allowlisted sender to any address
`@<yourdomain>`:

- **Text only** → goes in as `kind=prompt`. Subject becomes the prompt
  (or first non-empty line of body if subject is empty).
- **Image attached, no text** → `kind=image`. The image is converted to
  B&W and published as-is.
- **Image + subject** → `kind=image` with a prompt. Image is fed to
  gpt-image-1's edit endpoint with the prompt as a restyle hint.

Watch the Lambda log:

```sh
AWS_PROFILE=einkgen aws logs tail /aws/lambda/einkgen-inbound-email --follow
```

Then list the queue to confirm enqueue:

```sh
EINKGEN_BUCKET=einkgen-dev AWS_PROFILE=einkgen einkgen queue ls
```

#### 3.11.4. Managing the allowlist later

```sh
EINKGEN_BUCKET=einkgen-dev AWS_PROFILE=einkgen einkgen allowlist ls
EINKGEN_BUCKET=einkgen-dev AWS_PROFILE=einkgen einkgen allowlist add other@example.com
EINKGEN_BUCKET=einkgen-dev AWS_PROFILE=einkgen einkgen allowlist rm  other@example.com
```

Senders not on the list receive a friendly rejection email that never
names other allowed addresses; nothing is enqueued. Email matching is
case-insensitive on both sides.

---

## Part 4 — Day-2 ops

| Task | Command |
| --- | --- |
| Enqueue a prompt | `einkgen queue prompt "<text>"` |
| Enqueue an image | `einkgen queue image <path>` |
| Restyle an image | `einkgen queue image <path> --prompt "<text>"` |
| List the queue | `einkgen queue ls` |
| Cancel an item | `einkgen queue rm <id>` |
| List email allowlist | `einkgen allowlist ls` |
| Add/remove sender | `einkgen allowlist add\|rm <email>` |
| Tail generator logs | `aws logs tail /aws/lambda/einkgen-generator --follow` |
| Tail inbound-email logs | `aws logs tail /aws/lambda/einkgen-inbound-email --follow` |
| 24h ERROR sweep | `AWS_PROFILE=einkgen ./infra/scripts/check-errors.sh` |
| Rotate device token | `aws secretsmanager put-secret-value …` + reflash `secrets.h` |
| Deploy a code change | re-run §3.6 (web) and/or §3.7 (deploy) |
| Tear it all down | `( cd infra && npx cdk destroy -c env=dev )` |

`einkgen` CLI environment variables:

```
OPENAI_API_KEY=sk-...           # for `einkgen local generate/preview`
AWS_PROFILE=einkgen             # picked up by boto3
EINKGEN_BUCKET=einkgen-dev      # required for `einkgen queue *`
EINKGEN_CDN_BASE=https://…      # used to build manifest image_url
EINKGEN_CF_DISTRIBUTION_ID=…    # optional; enables CF invalidation on publish
```

`.env.example` documents the same set.

---

## Troubleshooting

- **`Cannot find module 'aws-cdk-lib'`** — run `npm install` inside
  `infra/`.
- **Docker daemon not running** — CDK Lambda bundling needs Docker.
  Start Docker Desktop or set `CDK_DOCKER` to a podman wrapper.
- **`Reserved concurrency cannot be greater than the account's available
  concurrency`** — your account's unreserved concurrency pool is too low.
  Either raise the account limit or lower the value in
  [infra/lib/lambdas.ts](infra/lib/lambdas.ts).
- **Web SPA loads but data tabs say "Could not load"** — the
  `VITE_READ_API_URL` baked into the build is stale or wrong. Re-run §3.6
  with the current outputs and §3.7 to redeploy.
- **`/status` returns 404 in the browser console** — expected before any
  device has reported. The SPA handles it (shows "Device has not reported
  yet."); the 404 disappears once `status/device-default.json` exists.
- **Generator Lambda 401s on OpenAI** — the secret wasn't populated.
  Re-run §3.5 and trigger another invocation
  (`einkgen queue prompt "..."` or wait for cron).
