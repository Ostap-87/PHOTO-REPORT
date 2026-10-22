#!/bin/bash
# ============================================================
#  ФотоОтчёт — установка на Mac (macOS 11+, Apple Silicon или Intel)
#  Запуск: двойной клик по файлу или в Терминале: bash install_mac.command
#  Что делает: проверяет Python 3.11+, ставит зависимости в папку .venv,
#  создаёт приложение «ФотоОтчёт» в ~/Applications (Launchpad / Finder / Dock).
#  Модель распознавания (~230 МБ) скачается сама при первом запуске.
# ============================================================
set -e
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
echo "=== ФотоОтчёт: установка в $APP_DIR ==="

PY=""
for c in python3.13 python3.12 python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 python3; do
    if command -v "$c" >/dev/null 2>&1; then
        ok=$("$c" -c 'import sys; print(sys.version_info >= (3, 11))' 2>/dev/null || echo False)
        if [ "$ok" = "True" ]; then PY="$(command -v "$c")"; break; fi
    fi
done
if [ -z "$PY" ]; then
    echo
    echo "Нужен Python 3.11 или новее. Сейчас откроется python.org:"
    echo "скачайте «macOS 64-bit universal2 installer», установите и запустите этот файл ещё раз."
    open "https://www.python.org/downloads/macos/"
    read -r -p "Нажмите Enter, чтобы закрыть…" _
    exit 1
fi
echo "[1/3] Python: $PY ($("$PY" --version))"

echo "[2/3] Устанавливаю зависимости (несколько минут, нужен интернет)…"
"$PY" -m venv .venv
# Медленный или недоступный pypi.org (Китай, часть сетей в России) — ставим с зеркала.
# Своё зеркало можно задать заранее: PIP_INDEX_URL=… bash install_mac.command
if [ -z "$PIP_INDEX_URL" ]; then
    if ! curl -s -m 8 -o /dev/null https://pypi.org/simple/pip/; then
        for m in https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.aliyun.com/pypi/simple/; do
            if curl -s -m 8 -o /dev/null "$m/pip/"; then export PIP_INDEX_URL="$m"; break; fi
        done
        [ -n "$PIP_INDEX_URL" ] && echo "      pypi.org отвечает медленно — использую зеркало $PIP_INDEX_URL"
    fi
fi
export PIP_DEFAULT_TIMEOUT="${PIP_DEFAULT_TIMEOUT:-60}"
./.venv/bin/python -m pip install -q --timeout 60 --retries 10 --upgrade pip
./.venv/bin/python -m pip install -q --timeout 60 --retries 10 -r requirements.txt

echo "[3/3] Создаю приложение «ФотоОтчёт» в ~/Applications"
APP="$HOME/Applications/ФотоОтчёт.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$HOME/Library/Logs"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>ФотоОтчёт</string>
  <key>CFBundleDisplayName</key><string>ФотоОтчёт</string>
  <key>CFBundleIdentifier</key><string>ru.photoreport.desktop</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>PhotoReport</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/PhotoReport" <<LAUNCH
#!/bin/bash
cd "$APP_DIR"
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/photo_gui.py" >> "\$HOME/Library/Logs/PhotoReport.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/PhotoReport"
touch "$APP"

echo
echo "ГОТОВО. Приложение «ФотоОтчёт» лежит в папке «Программы» вашего пользователя (~/Applications):"
echo "откройте его из Finder или Launchpad; чтобы держать под рукой — перетащите в Dock."
echo "Проекты хранятся в ~/Documents/ФотоОтчёт. Не переносите папку $APP_DIR — приложение запускается из неё."
open "$APP"
