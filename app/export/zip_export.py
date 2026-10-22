"""ZIP с фото, разложенными по папкам пунктов ТЗ.

Структура архива:
    01 1 Мебель и расстановка/1_01_IMG_0001.jpg
    02 1.1 Круглые банкетные столы диаметром 1,8 м — 12 шт/1.1_01_IMG_0002.jpg
    Неразобранное/001_IMG_0100.jpg

Имя папки — folder_name(item, index): порядковый номер пункта в ТЗ (для сортировки),
номер пункта и его краткое название; безопасные символы, не длиннее 80 знаков.
В архив попадают только пункты, к которым есть фото или файлы. Вложения (project["files"]:
pdf, docx, xlsx, видео) лежат в папке пункта рядом с фото под исходными именами; фото и файлы
без пункта — в «Неразобранное/».
"""

from __future__ import annotations

import os
import zipfile

from .common import PhotoIndex, ensure_ext_jpg, file_path, photo_path, safe_name

UNSORTED_FOLDER = "Неразобранное"
FOLDER_LIMIT = 80


def folder_name(item: dict, index: int | None = None, width: int = 2) -> str:
    """Имя папки пункта в ZIP, например «01 1.2 Расстановка столов».

    index — порядковый номер пункта в project["items"] начиная с 1 (так делает export_zip);
    без index — «1.2 Расстановка столов».
    """
    num = safe_name(item.get("num") or "", 20, fallback="")
    title = (item.get("title") or item.get("text") or "").strip() or "Пункт"
    prefix = f"{index:0{width}d}" if index is not None else ""
    head = " ".join(p for p in (prefix, num) if p)
    room = max(10, FOLDER_LIMIT - len(head) - 1) if head else FOLDER_LIMIT
    short = safe_name(title, room, fallback="Пункт")
    return safe_name(f"{head} {short}" if head else short, FOLDER_LIMIT)


def folder_names(project: dict) -> dict:
    """{item_id: имя папки} — те же имена, что внутри ZIP."""
    items = project.get("items") or []
    width = max(2, len(str(len(items))))
    return {it.get("id"): folder_name(it, i + 1, width) for i, it in enumerate(items)}


def _unique(name: str, used: set) -> str:
    if name.lower() not in used:
        used.add(name.lower())
        return name
    stem, ext = os.path.splitext(name)
    k = 2
    while f"{stem} ({k}){ext}".lower() in used:
        k += 1
    name = f"{stem} ({k}){ext}"
    used.add(name.lower())
    return name


def _file_name(prefix: str, seq: int, width: int, photo: dict, path: str, which: str) -> str:
    orig_name = photo.get("name") or os.path.basename(path)
    stem = safe_name(os.path.splitext(orig_name)[0], 60, fallback=photo.get("id") or "фото")
    ext = os.path.splitext(path)[1].lower() or ".jpg"
    if which != "original" and path.lower().endswith(".jpg"):
        name = ensure_ext_jpg(stem)
    else:
        name = stem + ext
    parts = [p for p in (prefix, f"{seq:0{width}d}") if p]
    return safe_name("_".join(parts + [name]), 120)


def export_zip(project: dict, project_dir: str, out_path: str, which: str = "work") -> str:
    """Собирает ZIP: папки «<№п/п> <№> <пункт>/» с фото по порядку item.photo_ids и «Неразобранное/».

    which = "work" — рабочие копии (улучшенные, со скрытыми лицами), "original" — исходники.
    """
    which = "original" if which == "original" else "work"
    items = project.get("items") or []
    idx = PhotoIndex(project)
    names = folder_names(project)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    used_dirs: set = set()
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for i, item in enumerate(items):
            photos = idx.for_item(item)
            files = [(p, photo_path(project_dir, p, which)) for p in photos]
            files = [(p, f) for p, f in files if f]
            attachments = [(f, file_path(project_dir, f)) for f in idx.files_for(item)]
            attachments = [(f, path) for f, path in attachments if path]
            if not files and not attachments:
                continue
            folder = _unique(names.get(item.get("id")) or folder_name(item, i + 1), used_dirs)
            num = safe_name(item.get("num") or "", 20, fallback="") or f"{i + 1:02d}"
            width = max(2, len(str(len(files))))
            used: set = set()
            for seq, (p, path) in enumerate(files, start=1):
                fname = _unique(_file_name(num, seq, width, p, path, which), used)
                zf.write(path, f"{folder}/{fname}")
            _write_attachments(zf, folder, attachments, used)
        loose = [(p, photo_path(project_dir, p, which)) for p in idx.unsorted()]
        loose = [(p, f) for p, f in loose if f]
        loose_files = [(f, file_path(project_dir, f)) for f in idx.unsorted_files()]
        loose_files = [(f, path) for f, path in loose_files if path]
        used = set()
        if loose:
            width = max(3, len(str(len(loose))))
            for seq, (p, path) in enumerate(loose, start=1):
                fname = _unique(_file_name("", seq, width, p, path, which), used)
                zf.write(path, f"{UNSORTED_FOLDER}/{fname}")
        _write_attachments(zf, UNSORTED_FOLDER, loose_files, used)
    return out_path


_PACKED = {".txt", ".csv", ".json", ".xml", ".html", ".htm", ".md", ".rtf", ".doc", ".xls", ".ppt", ".svg"}


def _write_attachments(zf: zipfile.ZipFile, folder: str, attachments: list, used: set) -> None:
    """Вложения (pdf, docx, видео…) — в папку пункта с исходными именами."""
    for f, path in attachments:
        ext = os.path.splitext(path)[1].lower()
        orig = f.get("name") or os.path.basename(path)
        stem = safe_name(os.path.splitext(orig)[0], 100, fallback=f.get("id") or "файл")
        fname = _unique(stem + (os.path.splitext(orig)[1].lower() or ext), used)
        zf.write(path, f"{folder}/{fname}",
                 compress_type=zipfile.ZIP_DEFLATED if ext in _PACKED else zipfile.ZIP_STORED)
