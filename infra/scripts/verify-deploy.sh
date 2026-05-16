#!/usr/bin/env bash
# Post-deploy verification: end-to-end checks against the live stack.
#
# Curl-only smoke test that catches the deployment failure modes we've
# actually hit:
#   1. SPA bundle built with no env vars → contains `localhost:3001` and
#      "Loading queue..." never resolves in the browser.
#   2. CloudFront drops the custom-domain alias → site root is unreachable.
#   3. Read-api Lambda regressed → /queue, /history, /status 5xx.
#   4. Admin behavior missing → /admin/me 403s from S3 instead of the API.
#   5. Generator never ran → /current/manifest.json 404.
#
# Reads the API + CDN URLs from the live CFN stack outputs so it works on
# any fresh worktree without cdk-outputs.json.
#
# Usage:
#   AWS_PROFILE=einkgen ./infra/scripts/verify-deploy.sh
#   AWS_PROFILE=einkgen ./infra/scripts/verify-deploy.sh --stack EinkgenStack-dev
set -euo pipefail

STACK="EinkgenStack-dev"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack) STACK="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--stack EinkgenStack-dev]"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

PASS=0
FAIL=0
WARN=0

pass() { printf "  \033[32m✓\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1"; FAIL=$((FAIL+1)); }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; WARN=$((WARN+1)); }
section() { printf "\n\033[1m== %s ==\033[0m\n" "$1"; }

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required tool: $1" >&2; exit 2; }
}
require curl
require jq
require aws

section "Stack outputs (${STACK})"
OUTPUTS_JSON=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].Outputs' --output json 2>/dev/null || true)
if [[ -z "$OUTPUTS_JSON" || "$OUTPUTS_JSON" == "null" ]]; then
  echo "Could not read outputs for stack $STACK" >&2
  exit 1
fi
get_out() {
  echo "$OUTPUTS_JSON" | jq -r --arg k "$1" '.[] | select(.OutputKey==$k) | .OutputValue'
}
READ_API_URL=$(get_out ReadApiUrl)
ADMIN_API_URL=$(get_out AdminApiUrl)
DEVICE_STATUS_URL=$(get_out DeviceStatusUrl)
CDN_DOMAIN=$(get_out CdnDomain)
CDN_DIST_ID=$(get_out CdnDistributionId)
BUCKET=$(get_out BucketName)

# Site domain: prefer the einkgenSiteDomain context from cdk.json (the canonical
# production hostname) when present, fall back to the CloudFront default.
SITE_DOMAIN=""
if [[ -f "infra/cdk.json" ]]; then
  SITE_DOMAIN=$(jq -r '.context.einkgenSiteDomain // empty' infra/cdk.json 2>/dev/null || true)
fi
[[ -z "$SITE_DOMAIN" ]] && SITE_DOMAIN="$CDN_DOMAIN"
SITE_URL="https://${SITE_DOMAIN}"

echo "  Site:      ${SITE_URL}"
echo "  Read API:  ${READ_API_URL}"
echo "  Admin API: ${ADMIN_API_URL}"
echo "  CDN:       ${CDN_DOMAIN}  (${CDN_DIST_ID})"
echo "  Bucket:    ${BUCKET}"

# ---------------------------------------------------------------------------
section "Read API (direct, bypassing CloudFront)"
# /queue → 200 {"items": [...]}
QUEUE_BODY=$(curl -sS -m 15 -o /tmp/verify-queue.json -w "%{http_code}" "${READ_API_URL}/queue" || echo "000")
if [[ "$QUEUE_BODY" == "200" ]] && jq -e '.items | type == "array"' /tmp/verify-queue.json >/dev/null 2>&1; then
  N=$(jq '.items | length' /tmp/verify-queue.json)
  pass "GET ${READ_API_URL}/queue → 200, items[$N]"
else
  fail "GET ${READ_API_URL}/queue → ${QUEUE_BODY} (body: $(head -c 200 /tmp/verify-queue.json 2>/dev/null))"
fi

HIST_CODE=$(curl -sS -m 15 -o /tmp/verify-history.json -w "%{http_code}" "${READ_API_URL}/history?limit=3" || echo "000")
if [[ "$HIST_CODE" == "200" ]] && jq -e '.items | type == "array"' /tmp/verify-history.json >/dev/null 2>&1; then
  N=$(jq '.items | length' /tmp/verify-history.json)
  pass "GET ${READ_API_URL}/history?limit=3 → 200, items[$N]"
else
  fail "GET ${READ_API_URL}/history → ${HIST_CODE}"
fi

STATUS_CODE=$(curl -sS -m 15 -o /tmp/verify-status.json -w "%{http_code}" "${READ_API_URL}/status" || echo "000")
case "$STATUS_CODE" in
  200) pass "GET ${READ_API_URL}/status → 200 (device has reported)" ;;
  404) pass "GET ${READ_API_URL}/status → 404 (no_status_yet, expected pre-device)" ;;
  *)   fail "GET ${READ_API_URL}/status → ${STATUS_CODE}" ;;
esac

# ---------------------------------------------------------------------------
section "Admin API (direct + via CloudFront)"
# Direct: unauthenticated GET /admin/me must be 401 (configured) or 503 (no
# password seeded). Anything else means the cookie middleware is broken.
ADMIN_DIRECT=$(curl -sS -m 15 -o /dev/null -w "%{http_code}" "${ADMIN_API_URL}/admin/me" || echo "000")
case "$ADMIN_DIRECT" in
  401) pass "GET ${ADMIN_API_URL}/admin/me → 401 (unauthenticated, password is set)" ;;
  503) warn "GET ${ADMIN_API_URL}/admin/me → 503 (admin password not configured — Admin tab will be unusable)" ;;
  *)   fail "GET ${ADMIN_API_URL}/admin/me → ${ADMIN_DIRECT}" ;;
esac

# Via CloudFront: /admin/* behavior must route to the admin API. Anything
# from `AmazonS3` server means the behavior is missing and the SPA's admin
# tab will silently fail.
ADMIN_VIA_CF_HEADERS=$(curl -sS -m 15 -D - -o /dev/null "${SITE_URL}/admin/me" || true)
ADMIN_VIA_CF_CODE=$(echo "$ADMIN_VIA_CF_HEADERS" | awk '/^HTTP/{print $2; exit}')
ADMIN_VIA_CF_SERVER=$(echo "$ADMIN_VIA_CF_HEADERS" | awk -F': ' 'tolower($1)=="server"{print tolower($2); exit}' | tr -d '\r')
if [[ "$ADMIN_VIA_CF_CODE" == "401" || "$ADMIN_VIA_CF_CODE" == "503" ]]; then
  pass "GET ${SITE_URL}/admin/me → ${ADMIN_VIA_CF_CODE} (CloudFront → Admin API)"
elif [[ "$ADMIN_VIA_CF_SERVER" == *"amazons3"* ]]; then
  fail "GET ${SITE_URL}/admin/me → S3 (admin/* behavior missing from CloudFront)"
else
  fail "GET ${SITE_URL}/admin/me → ${ADMIN_VIA_CF_CODE} (server: ${ADMIN_VIA_CF_SERVER})"
fi

# ---------------------------------------------------------------------------
section "CDN / device-facing manifest"
MANI_CODE=$(curl -sS -m 15 -o /tmp/verify-manifest.json -w "%{http_code}" "${SITE_URL}/current/manifest.json" || echo "000")
case "$MANI_CODE" in
  200)
    if jq -e '.image_url and .image_sha256 and .next_check_after' /tmp/verify-manifest.json >/dev/null 2>&1; then
      IMG_URL=$(jq -r '.image_url' /tmp/verify-manifest.json)
      SHA=$(jq -r '.image_sha256' /tmp/verify-manifest.json)
      NEXT=$(jq -r '.next_check_after' /tmp/verify-manifest.json)
      pass "GET ${SITE_URL}/current/manifest.json → 200 (sha=${SHA:0:12}…, next=${NEXT})"
      # Manifest's image_url must itself be reachable.
      IMG_CODE=$(curl -sS -m 30 -o /dev/null -w "%{http_code}" "$IMG_URL" || echo "000")
      IMG_CT=$(curl -sS -m 30 -I "$IMG_URL" 2>/dev/null | awk -F': ' 'tolower($1)=="content-type"{print tolower($2); exit}' | tr -d '\r')
      if [[ "$IMG_CODE" == "200" && "$IMG_CT" == *"bmp"* ]]; then
        pass "GET ${IMG_URL} → 200, ${IMG_CT}"
      else
        fail "GET ${IMG_URL} → ${IMG_CODE} (content-type: ${IMG_CT})"
      fi
    else
      fail "GET /current/manifest.json → 200 but missing required fields"
    fi
    ;;
  403|404)
    warn "GET ${SITE_URL}/current/manifest.json → ${MANI_CODE} (generator has never run; device will idle until first publish)"
    ;;
  *) fail "GET ${SITE_URL}/current/manifest.json → ${MANI_CODE}" ;;
esac

# ---------------------------------------------------------------------------
section "Web SPA (origin + bundle integrity)"
SHELL_HEADERS=$(curl -sS -m 15 -D - -o /tmp/verify-index.html "${SITE_URL}/" || true)
SHELL_CODE=$(echo "$SHELL_HEADERS" | awk '/^HTTP/{print $2; exit}')
if [[ "$SHELL_CODE" == "200" ]]; then
  pass "GET ${SITE_URL}/ → 200 (SPA shell)"
else
  fail "GET ${SITE_URL}/ → ${SHELL_CODE}"
fi

# Parse the JS bundle reference out of index.html.
JS_PATH=$(grep -oE 'src="[^"]+/assets/index-[A-Za-z0-9_-]+\.js"' /tmp/verify-index.html | head -1 | sed 's/^src="//; s/"$//')
if [[ -z "$JS_PATH" ]]; then
  fail "Could not find /assets/index-*.js in SPA shell (web/ not deployed?)"
else
  JS_URL="${SITE_URL}${JS_PATH}"
  JS_CODE=$(curl -sS -m 30 -o /tmp/verify-app.js -w "%{http_code}" "$JS_URL" || echo "000")
  if [[ "$JS_CODE" != "200" ]]; then
    fail "GET ${JS_URL} → ${JS_CODE}"
  else
    pass "GET ${JS_URL} → 200 ($(wc -c < /tmp/verify-app.js | tr -d ' ') bytes)"

    # Critical regression checks against the bundle.
    if grep -q 'localhost:' /tmp/verify-app.js; then
      fail "SPA bundle contains 'localhost:' — built without VITE_READ_API_URL/VITE_CDN_BASE; tabs will not load"
    else
      pass "SPA bundle has no 'localhost:' references"
    fi

    READ_API_HOST=$(echo "$READ_API_URL" | sed -E 's|^https?://||; s|/.*$||')
    if grep -q "$READ_API_HOST" /tmp/verify-app.js; then
      pass "SPA bundle references read-api host ${READ_API_HOST}"
    else
      fail "SPA bundle does NOT reference read-api host ${READ_API_HOST} — VITE_READ_API_URL was not set at build time"
    fi

    # CDN base: bundle should reference either the cloudfront default
    # or the custom site domain — whichever VITE_CDN_BASE was set to.
    if grep -qE "(${CDN_DOMAIN}|${SITE_DOMAIN})" /tmp/verify-app.js; then
      pass "SPA bundle references CDN host"
    else
      fail "SPA bundle does NOT reference CDN host (${CDN_DOMAIN} or ${SITE_DOMAIN})"
    fi
  fi
fi

# ---------------------------------------------------------------------------
section "Generator pipeline (Lambda config + recent errors)"
GEN_CFG=$(aws lambda get-function-configuration --function-name einkgen-generator \
  --query '{State:State,LastUpdateStatus:LastUpdateStatus,Runtime:Runtime,Memory:MemorySize,Timeout:Timeout}' \
  --output json 2>/dev/null || true)
if [[ -z "$GEN_CFG" || "$GEN_CFG" == "null" ]]; then
  fail "einkgen-generator Lambda not found"
else
  STATE=$(echo "$GEN_CFG" | jq -r '.State')
  LUS=$(echo "$GEN_CFG" | jq -r '.LastUpdateStatus')
  if [[ "$STATE" == "Active" && "$LUS" == "Successful" ]]; then
    pass "einkgen-generator: State=Active, LastUpdateStatus=Successful"
  else
    fail "einkgen-generator: State=${STATE}, LastUpdateStatus=${LUS}"
  fi
fi

# Sweep ERROR-level log lines in the last 30 min across all four Lambdas.
NOW=$(date +%s); BACK=$(( NOW - 1800 ))
ERR_TOTAL=0
for fn in einkgen-generator einkgen-read-api einkgen-device-status einkgen-admin-api; do
  QID=$(aws logs start-query --log-group-name "/aws/lambda/${fn}" \
    --start-time "$BACK" --end-time "$NOW" \
    --query-string 'fields @timestamp, @message | filter @message like /ERROR/ | limit 20' \
    --query 'queryId' --output text 2>/dev/null || echo "")
  [[ -z "$QID" ]] && continue
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    ST=$(aws logs get-query-results --query-id "$QID" --query 'status' --output text 2>/dev/null || echo "")
    [[ "$ST" == "Complete" || "$ST" == "Failed" || "$ST" == "Cancelled" ]] && break
    sleep 1
  done
  COUNT=$(aws logs get-query-results --query-id "$QID" --query 'length(results)' --output text 2>/dev/null || echo "0")
  if [[ "$COUNT" != "0" && -n "$COUNT" ]]; then
    warn "${fn}: ${COUNT} ERROR line(s) in last 30 min"
    ERR_TOTAL=$((ERR_TOTAL + COUNT))
  fi
done
[[ "$ERR_TOTAL" == "0" ]] && pass "No ERROR log lines in any Lambda (last 30 min)"

# ---------------------------------------------------------------------------
section "Result"
printf "  pass: \033[32m%d\033[0m   warn: \033[33m%d\033[0m   fail: \033[31m%d\033[0m\n" "$PASS" "$WARN" "$FAIL"
if [[ "$FAIL" -gt 0 ]]; then
  echo "FAIL"
  exit 1
fi
echo "OK"
