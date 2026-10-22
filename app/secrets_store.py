from __future__ import annotations
# -*- coding: utf-8 -*-
"""Ключ API для анализа ТЗ — только на этом компьютере, вне папки с проектами и вне репозитория.

Mac: ~/Library/Application Support/ФотоОтчёт/secrets.json, Windows: %APPDATA%\\ФотоОтчёт,
Linux: ~/.config/photo-report. Файл доступен только владельцу (права 600). Переменная
ANTHROPIC_API_KEY, если задана, используется, когда в настройках ключа нет.
"""
import json
import os
import sys


def config_dir() -> str:
    if os.environ.get("PHOTO_REPORT_CONFIG"):
        d = os.environ["PHOTO_REPORT_CONFIG"]
    elif sys.platform == "darwin":
        d = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "ФотоОтчёт")
    elif sys.platform.startswith("win"):
        d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "ФотоОтчёт")
    else:
        d = os.path.join(os.path.expanduser("~"), ".config", "photo-report")
    os.makedirs(d, exist_ok=True)
    return d


def _path() -> str:
    return os.path.join(config_dir(), "secrets.json")


def _read() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def get_api_key() -> str | None:
    return _read().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or None


def source() -> str | None:
    if _read().get("anthropic_api_key"):
        return "settings"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "env"
    return None


def masked() -> str:
    k = get_api_key() or ""
    return f"{k[:7]}…{k[-4:]}" if len(k) > 16 else ""


def _write(data: dict) -> None:
    p = _path()
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def set_api_key(key: str) -> None:
    data = _read()
    data["anthropic_api_key"] = key.strip()
    _write(data)


def clear_api_key() -> None:
    data = _read()
    data.pop("anthropic_api_key", None)
    if data:
        _write(data)
    else:
        try:
            os.remove(_path())
        except OSError:
            pass
