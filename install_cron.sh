#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRON_CMD="45 14 * * 1-5 cd ${ROOT_DIR} && /usr/bin/env bash ${ROOT_DIR}/run_daily.sh"

chmod +x "${ROOT_DIR}/run_daily.sh" "${ROOT_DIR}/install_cron.sh"

if crontab -l 2>/dev/null | grep -F "${ROOT_DIR}/run_daily.sh" >/dev/null; then
  echo "Cron already exists. Skip."
  exit 0
fi

# 无 crontab 时 crontab -l 会失败；必须 || true，否则 set -e 会中断，任务加不进去
(crontab -l 2>/dev/null || true; echo "${CRON_CMD}") | crontab -
echo "Installed cron:"
echo "${CRON_CMD}"
echo "Current crontab:"
crontab -l
