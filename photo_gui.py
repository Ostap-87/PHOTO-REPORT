# -*- coding: utf-8 -*-
"""ФотоОтчёт: окно приложения (pywebview) + встроенный сервер на свободном порту.

Запуск: python3 photo_gui.py  (на Mac — приложение «ФотоОтчёт» из «Программ», см. install_mac.command)
Проекты хранятся в ~/Documents/ФотоОтчёт/Проекты, папки с разложенными фото — в ~/Documents/ФотоОтчёт/Папки.
"""
import os
import socket
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("PHOTO_REPORT_HOME", os.path.join(os.path.expanduser("~"), "Documents", "ФотоОтчёт", "Проекты"))

import uvicorn  # noqa: E402


def free_port(preferred: int = 8010) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


PORT = free_port(int(os.environ.get("PHOTO_REPORT_PORT", "8010")))
URL = f"http://127.0.0.1:{PORT}"


def run_server():
    uvicorn.run("app.server:app", host="127.0.0.1", port=PORT, log_level="warning")


threading.Thread(target=run_server, daemon=True).start()

for _ in range(90):
    try:
        if b'"ok":true' in urllib.request.urlopen(URL + "/api/health", timeout=2).read():
            break
    except Exception:
        pass
    time.sleep(1)

try:
    import webview
except ImportError:                      # окна нет — открываем в браузере по умолчанию
    import webbrowser
    webbrowser.open(URL)
    print(f"ФотоОтчёт работает: {URL} (Ctrl+C — остановить)")
    while True:
        time.sleep(3600)

try:
    webview.settings["ALLOW_DOWNLOADS"] = True        # отчёты Word/PDF/Excel/ZIP сохраняются в «Загрузки»
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
except Exception:
    pass
window = webview.create_window("ФотоОтчёт", URL, width=1480, height=940, min_size=(1100, 720))
window.events.closed += lambda: os._exit(0)
webview.start()
os._exit(0)
