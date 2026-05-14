#!/usr/bin/env bash
# Manual error-check pass (Milestone 12).
# Runs CloudWatch Logs Insights against all three Lambdas for ERROR lines
# in the last 24h. Prints a summary per Lambda.
#
# Usage:
#   AWS_PROFILE=einkgen ./infra/scripts/check-errors.sh
#   AWS_PROFILE=einkgen ./infra/scripts/check-errors.sh 6h
set -euo pipefail

WINDOW="${1:-24h}"

case "$WINDOW" in
  *h) SECONDS_BACK=$(( ${WINDOW%h} * 3600 )) ;;
  *m) SECONDS_BACK=$(( ${WINDOW%m} * 60 )) ;;
  *d) SECONDS_BACK=$(( ${WINDOW%d} * 86400 )) ;;
  *) echo "Unknown window: $WINDOW (use Xh, Xm, or Xd)" >&2; exit 2 ;;
esac

NOW=$(date +%s)
START=$(( NOW - SECONDS_BACK ))

LAMBDAS=(
  "einkgen-generator"
  "einkgen-read-api"
  "einkgen-device-status"
)

QUERY='fields @timestamp, @message
| filter @message like /ERROR/
| sort @timestamp desc
| limit 100'

for fn in "${LAMBDAS[@]}"; do
  LG="/aws/lambda/${fn}"
  echo "=================================================="
  echo "Lambda: ${fn}"
  echo "Log group: ${LG}"
  echo "Window: last ${WINDOW} (from $(date -r $START -u +%FT%TZ))"
  echo "--------------------------------------------------"

  QID=$(aws logs start-query \
    --log-group-name "$LG" \
    --start-time "$START" \
    --end-time "$NOW" \
    --query-string "$QUERY" \
    --query 'queryId' --output text 2>/dev/null || echo "")

  if [[ -z "$QID" ]]; then
    echo "(log group missing or query failed; skipping)"
    continue
  fi

  while :; do
    STATUS=$(aws logs get-query-results --query-id "$QID" --query 'status' --output text)
    if [[ "$STATUS" == "Complete" || "$STATUS" == "Failed" || "$STATUS" == "Cancelled" ]]; then
      break
    fi
    sleep 1
  done

  aws logs get-query-results --query-id "$QID" \
    --query 'results[].[ [?field==`@timestamp`].value | [0], [?field==`@message`].value | [0] ]' \
    --output text || true
done

echo "=================================================="
echo "done."
