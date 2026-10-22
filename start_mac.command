#!/bin/bash
# ФотоОтчёт в браузере (если окно приложения не нужно): двойной клик, затем откроется Safari.
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Сначала запустите install_mac.command"; read -r -p "Enter…" _; exit 1; }
export PHOTO_REPORT_HOME="${PHOTO_REPORT_HOME:-$HOME/Documents/ФотоОтчёт/Проекты}"
( sleep 3; open "http://127.0.0.1:8010" ) &
echo "ФотоОтчёт работает: http://127.0.0.1:8010 — закройте это окно, чтобы остановить."
exec ./.venv/bin/python -m uvicorn app.server:app --host 127.0.0.1 --port 8010
