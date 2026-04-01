#!/usr/bin/env bash
# 建议在每交易日 14:45–14:50 之间跑一次：数据拉取约 2–3 分钟，约 14:50 出结果便于 14:55 前下单。
# 默认 14:45；若希望更接近 14:50 出结果，可把下面 CRON_CMD 里的 45 改成 50。
# 请确保系统时区为 Asia/Shanghai，并与 NTP 同步（如 timedatectl / chrony）。
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
