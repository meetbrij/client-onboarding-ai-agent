#!/usr/bin/env bash
# Set the Secrets Manager values for one environment, WITHOUT overwriting anything that already has a value.
#
#   scripts/set_secrets.sh qa                      # fill the secrets that are still empty; skip the ones that have a value
#   scripts/set_secrets.sh qa --dry-run            # show what would happen, change nothing
#   scripts/set_secrets.sh qa --force app-secret   # replace app-secret (prints the old version id and the roll-back command)
#   scripts/set_secrets.sh qa --force pg-secret --confirm-pg-secret-overwrite
#   KYC_API_KEY=... ANTHROPIC_API_KEY=... scripts/set_secrets.sh qa   # the two API keys come from the environment (never an argument)
#
# Why: Postgres reads its passwords only when its volume is first created. Overwriting <env>/onboarding/pg-secret later makes
# the next pod restart or deploy fail to connect (this happened once). The script refuses to do that by accident.
#
# It never reads a secret's value: it asks only whether a current version exists (describe-secret returns metadata).
# Needs: aws (credentials for the account), jq, openssl, and shasum or sha256sum. Passwords are letters and digits only (they
# go into connection URLs). Values are written from a private temporary file, never put on a command line or printed. The only
# thing printed is the three sign-in tokens, once, at the end, when app-secret was written: save them.
set -euo pipefail

REGION="${AWS_REGION:-ap-south-1}"
SECRETS=(pg-secret app-secret langfuse-keys kyc-api-key anthropic-api-key)

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

[ $# -ge 1 ] || usage 2
ENV="$1"; shift
case "$ENV" in qa|prod) ;; *) echo "environment must be qa or prod, not '$ENV'" >&2; exit 2 ;; esac

DRY=0; CONFIRM_PG=0; FORCE=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --confirm-pg-secret-overwrite) CONFIRM_PG=1 ;;
    --force) shift; [ $# -gt 0 ] || { echo "--force needs a secret name" >&2; exit 2; }
             case "$1" in pg-secret|app-secret|langfuse-keys|kyc-api-key|anthropic-api-key) FORCE+=("$1") ;; *) echo "unknown secret '$1'" >&2; exit 2 ;; esac ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument '$1'" >&2; usage 2 ;;
  esac
  shift
done

for tool in aws jq openssl; do command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 2; }; done
if command -v shasum >/dev/null; then sha() { printf '%s' "$1" | shasum -a 256 | cut -d' ' -f1; }
elif command -v sha256sum >/dev/null; then sha() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
else echo "shasum or sha256sum is required" >&2; exit 2; fi

pw() { openssl rand -hex 24; }
is_forced() { local s; for s in ${FORCE[@]+"${FORCE[@]}"}; do [ "$s" = "$1" ] && return 0; done; return 1; }

# Prints the id of the version labelled AWSCURRENT, or nothing if the secret has no value yet.
# Any other failure (no credentials, access denied, wrong name) stops the script.
current_version() {
  local out
  if ! out=$(aws secretsmanager describe-secret --secret-id "$1" --region "$REGION" --output json 2>&1); then
    echo "could not look up $1: $out" >&2
    return 1
  fi
  printf '%s' "$out" | jq -r '(.VersionIdsToStages // {}) | to_entries | map(select(.value | index("AWSCURRENT"))) | .[0].key // empty'
}

TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT; chmod 600 "$TMP"
PRINT_TOKENS=""

write_secret() {  # name, json
  local name="$1" json="$2" id="$ENV/onboarding/$1" old new
  old="$(current_version "$id")" || return 1
  if [ -n "$old" ] && ! is_forced "$name"; then
    echo "SKIPPED   $id: already has a value (version $old). To replace it, pass --force $name."
    return 0
  fi
  if [ -n "$old" ] && [ "$name" = "pg-secret" ] && [ "$CONFIRM_PG" -ne 1 ]; then
    echo "REFUSED   $id: overwriting it locks the API and mock bank out of Postgres, which keeps its original passwords." >&2
    echo "          If you are sure (for example the database does not exist yet), add --confirm-pg-secret-overwrite." >&2
    return 3
  fi
  if [ -z "$json" ]; then
    echo "NOT SET   $id: the matching environment variable is empty; export it and run again."
    return 0
  fi
  if [ "$DRY" -eq 1 ]; then
    echo "DRY RUN   would write $id${old:+ (replacing version $old)}"
    return 0
  fi
  umask 077; printf '%s' "$json" > "$TMP"
  new=$(aws secretsmanager put-secret-value --secret-id "$id" --region "$REGION" --secret-string "file://$TMP" --query VersionId --output text)
  if [ -n "$old" ]; then
    echo "REPLACED  $id: old version $old, new version $new."
    echo "          To roll back: aws secretsmanager update-secret-version-stage --secret-id $id --region $REGION \\"
    echo "            --version-stage AWSCURRENT --move-to-version-id $old --remove-from-version-id $new"
  else
    echo "WRITTEN   $id (version $new)."
  fi
  [ "$name" = "app-secret" ] && PRINT_TOKENS=1
  return 0
}

status=0
for name in "${SECRETS[@]}"; do
  case "$name" in
    pg-secret)
      json=$(jq -n --arg su "$(pw)" --arg owner "$(pw)" --arg app "$(pw)" --arg bank "$(pw)" \
        '{POSTGRES_PASSWORD:$su, ONBOARDING_OWNER_PASSWORD:$owner, ONBOARDING_APP_PASSWORD:$app, MOCKBANK_PASSWORD:$bank}') ;;
    app-secret)
      O1="$(pw)"; O2="$(pw)"; SUB="$(pw)"; SMOKE="$(pw)"
      tokens=$(jq -cn --arg o1 "$(sha "$O1")" --arg o2 "$(sha "$O2")" --arg s "$(sha "$SUB")" --arg sm "$(sha "$SMOKE")" \
        '[{id:"officer-1",role:"officer",sha256:$o1},{id:"officer-2",role:"officer",sha256:$o2},{id:"submitter-1",role:"submitter",sha256:$s},{id:"smoke-bot",role:"submitter",sha256:$sm}]')
      json=$(jq -n --arg tokens "$tokens" --arg session "$(pw)" --arg smoke "$SMOKE" \
        '{ONBOARDING_TOKENS:$tokens, SESSION_SECRET:$session, SMOKE_TOKEN:$smoke}') ;;
    langfuse-keys)
      json=$(jq -n --arg pub "${LANGFUSE_PUBLIC_KEY:-}" --arg sec "${LANGFUSE_SECRET_KEY:-}" \
        '{LANGFUSE_PUBLIC_KEY:$pub, LANGFUSE_SECRET_KEY:$sec}') ;;
    kyc-api-key)
      json=""; [ -n "${KYC_API_KEY:-}" ] && json=$(jq -n --arg k "$KYC_API_KEY" '{KYC_API_KEY:$k}') ;;
    anthropic-api-key)
      json=""; [ -n "${ANTHROPIC_API_KEY:-}" ] && json=$(jq -n --arg k "$ANTHROPIC_API_KEY" '{ANTHROPIC_API_KEY:$k}') ;;
  esac
  write_secret "$name" "$json" || status=$?
done

if [ -n "$PRINT_TOKENS" ]; then
  echo
  echo "Sign-in tokens for $ENV. They are shown ONCE and only their hashes are stored. Save them now:"
  echo "  officer-1:   $O1"
  echo "  officer-2:   $O2"
  echo "  submitter-1: $SUB"
fi
exit "$status"
