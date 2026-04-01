#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

mkdir -p logs trade_logs

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -d ".venv" ]]; then
  PYTHON_BIN=".venv/bin/python"
fi

{
  echo "[$(date '+%F %T')] start run_daily"
  "$PYTHON_BIN" main.py
  "$PYTHON_BIN" send_email.py || true
  echo "[$(date '+%F %T')] end run_daily"
} >> logs/run.log 2>&1
