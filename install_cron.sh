#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRON_CMD="45 14 * * 1-5 cd ${ROOT_DIR} && /usr/bin/env bash ${ROOT_DIR}/run_daily.sh"

chmod +x "${ROOT_DIR}/run_daily.sh" "${ROOT_DIR}/install_cron.sh"

if crontab -l 2>/dev/null | grep -F "${ROOT_DIR}/run_daily.sh" >/dev/null; then
  echo "Cron already exists. Skip."
  exit 0
fi

(crontab -l 2>/dev/null; echo "${CRON_CMD}") | crontab -
echo "Installed cron:"
echo "${CRON_CMD}"
