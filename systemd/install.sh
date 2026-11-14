#!/bin/bash
# Установка штатного надзора duo-pipeline (идемпотентно): user-таймер `duo-supervise` вместо
# прежних watchdog.sh (живой bash-цикл) + watchdog_failsafe.sh (одноходовка, ломавшаяся
# systemd'ом). Старый duo-watchdog-failsafe.timer отключается.
# Использование: bash /home/sawa/GitHub/vca-platform/tools/dev/duo_pipeline/systemd/install.sh
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="$HOME/.config/systemd/user"
mkdir -p "$DST"
if systemctl --user list-unit-files duo-watchdog-failsafe.timer >/dev/null 2>&1; then
    systemctl --user disable --now duo-watchdog-failsafe.timer 2>/dev/null || true
    rm -f "$DST/duo-watchdog-failsafe.service" "$DST/duo-watchdog-failsafe.timer"
fi
install -m 0644 "$SRC/duo-supervise.service" "$DST/duo-supervise.service"
install -m 0644 "$SRC/duo-supervise.timer" "$DST/duo-supervise.timer"
systemctl --user daemon-reload
systemctl --user enable --now duo-supervise.timer
systemctl --user list-timers 'duo-supervise*' --no-pager
echo "OK. Проверка: journalctl --user -u duo-supervise.service -n 5 --no-pager"
