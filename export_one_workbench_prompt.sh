#!/usr/bin/env bash
set -euo pipefail

# MVP exporter for one Claude Platform workbench prompt + all full revision versions.
# Requires an active logged-in Playwright browser session.

ORG_ID="${1:-}"
PROMPT_ID="${2:-}"
OUTPUT_ROOT="${3:-output/workbench-export}"

if [[ -z "${ORG_ID}" || -z "${PROMPT_ID}" ]]; then
  cat <<'USAGE' >&2
Usage:
  ./export_one_workbench_prompt.sh <org_id> <prompt_id> [output_root_dir]

Example:
  ./export_one_workbench_prompt.sh \
    97c46a4f-04cc-49b0-80a9-cb32caa4acc0 \
    be781bfa-cf90-4ab9-9941-f1ab1d0fa86c
USAGE
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required." >&2
  exit 1
fi

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
PWCLI="$CODEX_HOME/skills/playwright/scripts/playwright_cli.sh"

if [[ ! -f "$PWCLI" ]]; then
  echo "Error: Playwright wrapper not found at: $PWCLI" >&2
  exit 1
fi

mkdir -p "$OUTPUT_ROOT"

cookie_dump="$(NPM_CONFIG_CACHE=/tmp/.npm-cache bash "$PWCLI" cookie-list)"
session_key="$(printf '%s\n' "$cookie_dump" | sed -n 's/^sessionKey=\(.*\) (domain: \.platform\.claude\.com, path: \/)$/\1/p' | head -n1)"
routing_hint="$(printf '%s\n' "$cookie_dump" | sed -n 's/^routingHint=\(.*\) (domain: \.platform\.claude\.com, path: \/)$/\1/p' | head -n1)"

if [[ -z "${session_key}" || -z "${routing_hint}" ]]; then
  echo "Error: could not extract session cookies from Playwright browser session." >&2
  echo "Make sure the visible browser is still logged into platform.claude.com." >&2
  exit 1
fi

api_get() {
  local path="$1"
  curl -sS "https://platform.claude.com${path}" \
    -H 'accept: application/json' \
    -H 'x-requested-with: XMLHttpRequest' \
    -H "cookie: sessionKey=${session_key}; routingHint=${routing_hint}"
}

prompt_json="$(api_get "/api/organizations/${ORG_ID}/workbench/prompts/${PROMPT_ID}")"
prompt_name="$(printf '%s\n' "$prompt_json" | jq -r '.name // "untitled"')"
prompt_slug="$(printf '%s' "$prompt_name" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/--+/-/g')"
if [[ -z "$prompt_slug" ]]; then
  prompt_slug="untitled"
fi

prompt_dir="${OUTPUT_ROOT}/${prompt_slug}-${PROMPT_ID}"
revisions_dir="${prompt_dir}/revisions"
evaluations_dir="${prompt_dir}/evaluations"
mkdir -p "$revisions_dir" "$evaluations_dir"

printf '%s\n' "$prompt_json" | jq '.' > "${prompt_dir}/prompt.json"

revisions_compact_json="$(api_get "/api/organizations/${ORG_ID}/workbench/prompts/${PROMPT_ID}/revisions?compact=true")"
revision_count=0
revision_ids_json='[]'
evaluation_file_count=0
evaluation_total_count=0
evaluation_counts_json='[]'

while IFS= read -r rev_id; do
  [[ -z "${rev_id}" ]] && continue
  rev_json="$(api_get "/api/organizations/${ORG_ID}/workbench/prompts/${PROMPT_ID}/revisions/${rev_id}")"
  printf '%s\n' "$rev_json" | jq '.' > "${revisions_dir}/${rev_id}.json"

  evals_json="$(api_get "/api/organizations/${ORG_ID}/workbench/revisions/${rev_id}/evaluations/list")"
  printf '%s\n' "$evals_json" | jq '.' > "${evaluations_dir}/${rev_id}.json"
  eval_count_this_revision="$(printf '%s\n' "$evals_json" | jq 'length')"
  evaluation_total_count=$((evaluation_total_count + eval_count_this_revision))
  evaluation_file_count=$((evaluation_file_count + 1))
  evaluation_counts_json="$(
    jq -c \
      --arg revision_id "$rev_id" \
      --argjson count "$eval_count_this_revision" \
      '. + [{revision_id: $revision_id, count: $count}]' \
      <<<"$evaluation_counts_json"
  )"

  revision_count=$((revision_count + 1))
  revision_ids_json="$(jq -c --arg id "$rev_id" '. + [$id]' <<<"$revision_ids_json")"
done < <(printf '%s\n' "$revisions_compact_json" | jq -r '.[].id')

jq -n \
  --arg exported_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
  --arg source "platform.claude.com/workbench" \
  --arg org_id "$ORG_ID" \
  --arg prompt_id "$PROMPT_ID" \
  --arg prompt_name "$prompt_name" \
  --argjson revision_ids "$revision_ids_json" \
  --argjson revision_count "$revision_count" \
  --argjson evaluation_file_count "$evaluation_file_count" \
  --argjson evaluation_total_count "$evaluation_total_count" \
  --argjson evaluation_counts "$evaluation_counts_json" \
  '{
    exported_at: $exported_at,
    source: $source,
    organization_id: $org_id,
    prompt_id: $prompt_id,
    prompt_name: $prompt_name,
    revision_count: $revision_count,
    revision_ids: $revision_ids,
    evaluation_file_count: $evaluation_file_count,
    evaluation_total_count: $evaluation_total_count,
    evaluation_counts: $evaluation_counts
  }' > "${prompt_dir}/manifest.json"

echo "Exported prompt to directory: ${prompt_dir}"
echo "Revision files written: ${revision_count}"
echo "Evaluation files written: ${evaluation_file_count}"
echo "Total evaluations exported: ${evaluation_total_count}"
