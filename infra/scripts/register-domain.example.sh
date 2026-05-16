#!/usr/bin/env bash
# Registers einkgen.click via Route 53 Domains.
#
# Usage:
#   1. Edit the CONTACT block below — ICANN requires real registrant info.
#   2. ./infra/scripts/register-domain.sh
#   3. Wait 5–30min for the registration operation to complete; status:
#        aws route53domains get-operation-detail --operation-id <id> \
#          --region us-east-1 --profile einkgen
#
# Route 53 Domains is only available in us-east-1.
# Privacy protection (WHOIS masking) is enabled by default below.

set -euo pipefail

DOMAIN="REPLACE_WITH_DOMAIN_NAME"
PROFILE="einkgen"
REGION="us-east-1"
DURATION_YEARS=1
AUTO_RENEW=true

# ---------------------------------------------------------------------------
# Registrant / admin / tech contact — ICANN-required. All three default to
# the same person for a personal project. Privacy masking hides this from
# public WHOIS (still visible to ICANN + registrar).
# ---------------------------------------------------------------------------
CONTACT=$(cat <<'JSON'
{
  "FirstName": "REPLACE_WITH_FIRST_NAME",
  "LastName": "REPLACE_WITH_LAST_NAME",
  "ContactType": "PERSON",
  "AddressLine1": "REPLACE_WITH_STREET_ADDRESS",
  "City": "REPLACE_WITH_CITY",
  "State": "REPLACE_WITH_STATE",
  "CountryCode": "US",
  "ZipCode": "REPLACE_WITH_ZIP",
  "PhoneNumber": "+1.REPLACE_WITH_TEN_DIGITS",
  "Email": "REPLACE_WITH_EMAIL"
}
JSON
)

if echo "$CONTACT" | grep -q REPLACE_WITH; then
  echo "ERROR: edit the CONTACT block in $0 first — ICANN requires real contact info." >&2
  exit 1
fi

# Final availability check — guard against a race between the original
# availability scan and the registration attempt.
echo "Checking availability of $DOMAIN..."
status=$(aws route53domains check-domain-availability \
  --domain-name "$DOMAIN" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --output text)
if [ "$status" != "AVAILABLE" ]; then
  echo "ERROR: $DOMAIN is $status (was AVAILABLE earlier)." >&2
  exit 1
fi

echo "Registering $DOMAIN for $DURATION_YEARS year(s)..."
op=$(aws route53domains register-domain \
  --domain-name "$DOMAIN" \
  --duration-in-years "$DURATION_YEARS" \
  --auto-renew \
  --admin-contact "$CONTACT" \
  --registrant-contact "$CONTACT" \
  --tech-contact "$CONTACT" \
  --privacy-protect-admin-contact \
  --privacy-protect-registrant-contact \
  --privacy-protect-tech-contact \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query OperationId \
  --output text)

echo "Operation submitted: $op"
echo "Track with:"
echo "  aws route53domains get-operation-detail --operation-id $op --region $REGION --profile $PROFILE"
echo
echo "Once status=SUCCESSFUL (typically 5–30min), tell Claude to continue."
