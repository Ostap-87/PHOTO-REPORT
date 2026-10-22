@echo off
rem ФотоОтчёт на Windows: первый запуск ставит зависимости, затем открывает окно приложения.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Устанавливаю зависимости...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
.venv\Scripts\python photo_gui.py
