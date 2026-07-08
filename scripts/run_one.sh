#!/usr/bin/env bash
set -euo pipefail

uv run autosfm-orchestrator run-one \
  --config "${AUTOSFM_CONFIG:-conf/default.yaml}" \
  --batch-id "$1" \
  --start-time "$2" \
  --end-time "$3"
