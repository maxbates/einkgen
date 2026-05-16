#!/usr/bin/env bash
# Safe end-to-end deploy. The one canonical path that prevents the failure
# modes we've actually hit:
#
#   1. SPA built with no env vars → `localhost:3001` baked in →
#      "Loading queue..." spinner that never resolves. We've shipped this
#      to prod at least twice. Fix: always regenerate `web/.env.production`
#      from the LIVE stack outputs immediately before `npm run build`.
#   2. cdk-outputs.json missing from a fresh worktree → §3.6 of QUICKSTART
#      can't run as-written. Fix: pull outputs from CFN directly.
#   3. cdk.json context stripped → CdnSite alias deleted (CLAUDE.md hard
#      rule). Fix: never pass empty -c overrides; only the inputs we want
#      to change.
#
# Usage:
#   AWS_PROFILE=einkgen ./infra/scripts/deploy.sh
#   AWS_PROFILE=einkgen ./infra/scripts/deploy.sh --no-web    # infra-only
#   AWS_PROFILE=einkgen ./infra/scripts/deploy.sh --no-verify # skip post-deploy verify
#   AWS_PROFILE=einkgen ./infra/scripts/deploy.sh --stack EinkgenStack-prod
#
# After a successful deploy the script runs ./infra/scripts/verify-deploy.sh
# and exits non-zero if anything regressed.
set -euo pipefail

STACK="EinkgenStack-dev"
INCLUDE_WEB=1
RUN_VERIFY=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack) STACK="$2"; shift 2 ;;
    --no-web) INCLUDE_WEB=0; shift ;;
    --no-verify) RUN_VERIFY=0; shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO_ROOT"

# We need the stack to already exist so we can read its outputs (URLs)
# before rebuilding web. First-deploy from scratch should go through
# QUICKSTART §3.1–§3.5; this script is for redeploys / iterative deploys.
OUTPUTS_JSON=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].Outputs' --output json 2>/dev/null || true)
if [[ -z "$OUTPUTS_JSON" || "$OUTPUTS_JSON" == "null" ]]; then
  echo "ERROR: stack $STACK does not exist yet." >&2
  echo "       For the very first deploy follow QUICKSTART §3.4 → §3.7." >&2
  echo "       This script handles redeploys of an already-bootstrapped stack." >&2
  exit 1
fi
get_out() {
  echo "$OUTPUTS_JSON" | jq -r --arg k "$1" '.[] | select(.OutputKey==$k) | .OutputValue'
}
READ_API_URL=$(get_out ReadApiUrl)
CDN_DOMAIN=$(get_out CdnDomain)

# Use the custom site domain for VITE_CDN_BASE if one is configured, so
# the SPA fetches /current/manifest.json from the same origin it lives on
# (avoids needless cross-origin requests).
SITE_DOMAIN=$(jq -r '.context.einkgenSiteDomain // empty' infra/cdk.json 2>/dev/null || true)
CDN_BASE_HOST="${SITE_DOMAIN:-$CDN_DOMAIN}"

if [[ "$INCLUDE_WEB" == "1" ]]; then
  echo "==> Rebuilding web/ with live stack URLs"
  echo "    READ_API_URL = ${READ_API_URL}"
  echo "    CDN_BASE     = https://${CDN_BASE_HOST}"

  cat > web/.env.production <<EOF
VITE_READ_API_URL=${READ_API_URL}
VITE_CDN_BASE=https://${CDN_BASE_HOST}
EOF

  # Preserve any extra opt-in keys (e.g. VITE_INBOUND_EMAIL_DOMAIN) from a
  # pre-existing .env.production. We only own the two keys above.
  if [[ -f web/.env.production.local ]]; then
    cat web/.env.production.local >> web/.env.production
  fi

  ( cd web && npm install --silent && npm run build )

  # Sanity-check the freshly-built bundle BEFORE we ship it. Catches the
  # exact regression that motivated this script.
  if grep -RIl --include='*.js' 'localhost:' web/dist >/dev/null 2>&1; then
    echo "ERROR: built bundle contains 'localhost:' — env vars didn't take." >&2
    grep -RIn --include='*.js' 'localhost:' web/dist | head -5 >&2
    exit 1
  fi
  READ_API_HOST=$(echo "$READ_API_URL" | sed -E 's|^https?://||; s|/.*$||')
  if ! grep -Rq --include='*.js' "$READ_API_HOST" web/dist; then
    echo "ERROR: built bundle does not reference ${READ_API_HOST}." >&2
    exit 1
  fi
  echo "    bundle OK (refs ${READ_API_HOST}, no localhost)"
fi

echo "==> cdk deploy (stack from cdk.json env context, currently → ${STACK})"
CDK_ARGS=(
  "--outputs-file" "cdk-outputs.json"
  "--require-approval" "never"
)
if [[ "$INCLUDE_WEB" == "1" ]]; then
  CDK_ARGS+=("-c" "includeWebAssets=true")
fi
# NOTE: we deliberately do NOT pass `-c env=...` or any domain overrides.
# The canonical domain context lives in infra/cdk.json — overriding it
# from the CLI risks stripping CdnSite aliases (see CLAUDE.md hard rule).
# The CDK app produces exactly one stack per invocation (based on cdk.json
# `env`), so no positional stack arg is needed.
( cd infra && npx cdk deploy "${CDK_ARGS[@]}" )

if [[ "$RUN_VERIFY" == "1" ]]; then
  echo ""
  echo "==> Running post-deploy verification"
  ./infra/scripts/verify-deploy.sh --stack "$STACK"
fi
