#!/usr/bin/env bash
set -euo pipefail

# Backwards-compatible wrapper around the production Python exporter.

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
  echo "Error: jq is required by the exporter runtime." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is required." >&2
  exit 1
fi

exec python3 "$(dirname "$0")/export_workbench.py" \
  --org-id "$ORG_ID" \
  --prompt-id "$PROMPT_ID" \
  --output-root "$OUTPUT_ROOT" \
  --workers 1
