#!/usr/bin/env bash
set -euo pipefail

uv run autosfm-orchestrator run-log \
  --config "${AUTOSFM_CONFIG:-conf/default.yaml}" \
  --manual-log "$1"
