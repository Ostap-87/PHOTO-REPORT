from __future__ import annotations
# -*- coding: utf-8 -*-
"""Откуда берутся фото, кроме загрузки файлов в окне: папка на компьютере или флешке, ссылка на
Яндекс Диск, Google Диск, Dropbox, Облако Mail.ru (публичная) или прямая ссылка на файл/ZIP.

iter_path(path)            -> (имя, байты) картинок из файла, ZIP или папки (рекурсивно)
download(url, dest, note)  -> [пути скачанных файлов] (ZIP папки Яндекс Диска, файлы папки Google Диска…)
Ссылки должны быть открыты «для всех, у кого есть ссылка» — ключи и вход в аккаунт не нужны.
"""
import html
import os
import re
import urllib.parse
import zipfile
from typing import Callable, Iterator

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp"}
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) PhotoReport/1.0"}
MAX_FILES = 20000


class SourceError(ValueError):
    """Понятная пользователю ошибка источника (по-русски)."""


# ------------------------------------------------------------------ папка, файл, ZIP
def _skip(name: str) -> bool:
    base = os.path.basename(name)
    return base.startswith(".") or "__MACOSX" in name or base.lower() in ("thumbs.db", "desktop.ini")


def iter_path(path: str) -> Iterator[tuple[str, bytes]]:
    """Картинки из файла, ZIP-архива или папки (с подпапками). Имена — относительные, с подпапкой."""
    path = os.path.expanduser(path)
    if os.path.isdir(path):
        count = 0
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for f in sorted(files):
                full = os.path.join(root, f)
                if _skip(full):
                    continue
                sub = os.path.dirname(os.path.relpath(full, path))
                for name, data in iter_path(full):          # имя — с подпапкой: «День 1/IMG_0001.jpg»
                    count += 1
                    if count > MAX_FILES:
                        return
                    yield (f"{sub}/{name}" if sub else name), data
        return
    if not os.path.isfile(path):
        raise SourceError(f"Нет такой папки или файла: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".zip":
        try:
            zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise SourceError(f"Архив повреждён: {os.path.basename(path)}") from exc
        with zf:
            for info in zf.infolist():
                if info.is_dir() or _skip(info.filename):
                    continue
                if os.path.splitext(info.filename)[1].lower() in IMAGE_EXT:
                    yield os.path.basename(path) + "/" + info.filename, zf.read(info)
    elif ext in IMAGE_EXT:
        with open(path, "rb") as fh:
            yield os.path.basename(path), fh.read()


# ------------------------------------------------------------------ ссылки
def kind_of(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(h in host for h in ("disk.yandex.", "yadi.sk", "disk.360.yandex")):
        return "yandex"
    if "drive.google." in host or "docs.google." in host or "drive.usercontent.google." in host:
        return "gdrive"
    if "dropbox.com" in host:
        return "dropbox"
    if "cloud.mail.ru" in host:
        return "mailru"
    return "direct"


def _session():
    import requests
    s = requests.Session()
    s.headers.update(UA)
    return s


def _save(resp, dest: str, fallback: str, note: Callable[[str], None] | None, label: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
    name = urllib.parse.unquote(m.group(1)) if m else fallback
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", name).strip() or "file"
    if not os.path.splitext(name)[1]:
        ctype = resp.headers.get("Content-Type", "")
        name += ".zip" if "zip" in ctype else ".jpg" if "jpeg" in ctype else ".png" if "png" in ctype else ""
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, name)
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path = f"{base} ({n}){ext}"
        n += 1
    total = int(resp.headers.get("Content-Length") or 0)
    got = 0
    with open(path, "wb") as fh:
        for chunk in resp.iter_content(1 << 20):
            if chunk:
                fh.write(chunk)
                got += len(chunk)
                if note:
                    mb = got / 1048576
                    note(f"{label}: {mb:.0f} МБ" + (f" из {total / 1048576:.0f} МБ" if total else ""))
    return path


def _get(s, url: str, **kw):
    import requests
    try:
        r = s.get(url, stream=True, timeout=(20, 120), allow_redirects=True, **kw)
    except requests.RequestException as exc:
        raise SourceError(f"Нет связи с {urllib.parse.urlparse(url).netloc}: проверьте интернет") from exc
    if r.status_code in (401, 403):
        raise SourceError("Доступ закрыт: откройте ссылку «для всех, у кого есть ссылка»")
    if r.status_code == 404:
        raise SourceError("По ссылке ничего не найдено: проверьте адрес")
    if r.status_code >= 400:
        raise SourceError(f"Сервис ответил ошибкой {r.status_code}")
    return r


def _yandex(url: str, dest: str, note) -> list[str]:
    s = _session()
    api = "https://cloud-api.yandex.net/v1/disk/public/resources/download?public_key=" + urllib.parse.quote(url, safe="")
    r = _get(s, api)
    href = (r.json() or {}).get("href")
    if not href:
        raise SourceError("Яндекс Диск не отдал ссылку на скачивание")
    if note:
        note("Яндекс Диск: скачиваю (папка приходит ZIP-архивом)")
    return [_save(_get(s, href), dest, "yandex-disk.zip", note, "Яндекс Диск")]


_GD_FILE = re.compile(r"(?:/file/d/|[?&]id=|/open\?id=)([\w-]{10,})")
_GD_FOLDER = re.compile(r"/folders/([\w-]{10,})")


def parse_gdrive_folder(page: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Страница embeddedfolderview -> ([(id файла, имя)], [id подпапок])."""
    files, folders = [], []
    for chunk in page.split('<div class="flip-entry"')[1:]:      # каждая запись — до начала следующей
        m = re.match(r'\s+id="entry-([\w-]+)"', chunk)
        t = re.search(r'<div class="flip-entry-title">(.*?)</div>', chunk, re.S)
        if not m:
            continue
        name = html.unescape(re.sub(r"<[^>]+>", "", t.group(1))).strip() if t else ""
        if "/folders/" in chunk:
            folders.append(m.group(1))
        else:
            files.append((m.group(1), name))
    return files, folders


def _gdrive(url: str, dest: str, note) -> list[str]:
    s = _session()
    fm = _GD_FOLDER.search(url)
    if not fm:
        m = _GD_FILE.search(url)
        if not m:
            raise SourceError("Не понял ссылку Google Диска: нужна ссылка на файл или папку")
        return [_gdrive_file(s, m.group(1), "", dest, note)]
    out, queue, seen = [], [(fm.group(1), dest)], set()
    while queue:
        fid, where = queue.pop(0)
        if fid in seen:
            continue
        seen.add(fid)
        page = _get(s, f"https://drive.google.com/embeddedfolderview?id={fid}").text
        files, folders = parse_gdrive_folder(page)
        if not files and not folders and not out:
            raise SourceError("Папка Google Диска пуста или закрыта: откройте доступ «для всех, у кого есть ссылка»")
        for i, (f_id, name) in enumerate(files, 1):
            if os.path.splitext(name)[1].lower() in IMAGE_EXT | {".zip"}:
                if note:
                    note(f"Google Диск: файл {len(out) + 1} — {name}")
                out.append(_gdrive_file(s, f_id, name, where, None))
        queue += [(f, where) for f in folders]
    return out


def _gdrive_file(s, fid: str, name: str, dest: str, note) -> str:
    r = _get(s, f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t")
    if "text/html" in r.headers.get("Content-Type", ""):
        raise SourceError("Google Диск не отдал файл: откройте доступ «для всех, у кого есть ссылка»")
    return _save(r, dest, name or f"{fid}.jpg", note, "Google Диск")


def download(url: str, dest: str, note: Callable[[str], None] | None = None) -> list[str]:
    """Скачать по ссылке в папку dest -> список скачанных файлов (картинки и ZIP)."""
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        raise SourceError("Нужна ссылка, начинающаяся с http:// или https://")
    kind = kind_of(url)
    if kind == "yandex":
        return _yandex(url, dest, note)
    if kind == "gdrive":
        return _gdrive(url, dest, note)
    s = _session()
    if kind == "dropbox":
        url = re.sub(r"([?&])dl=0", r"\1dl=1", url) if "dl=0" in url else url + ("&" if "?" in url else "?") + "dl=1"
    if kind == "mailru":
        raise SourceError("Облако Mail.ru пока не поддерживается: скачайте архив и загрузите его файлом")
    return [_save(_get(s, url), dest, os.path.basename(urllib.parse.urlparse(url).path) or "download", note,
                  "Скачивание")]
