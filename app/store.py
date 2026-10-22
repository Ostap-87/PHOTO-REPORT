from __future__ import annotations
# -*- coding: utf-8 -*-
"""Хранение проектов на диске: storage/projects/<id>/project.json + файлы ТЗ и фото.

Папка данных: переменная PHOTO_REPORT_HOME или <приложение>/storage/projects.
Все изменения project.json — под блокировкой проекта и атомарной записью (tmp + replace).
Структура project.json — docs/ARCHITECTURE.md.
"""
import json
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import contextmanager

APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATUSES = {"todo": "Не начато", "done": "Выполнено", "provided": "Предоставлено",
            "executed": "Исполнено", "na": "Не требуется"}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_ID_RX = re.compile(r"^p\d{8}-[0-9a-f]{4,8}$")


def data_dir() -> str:
    d = os.environ.get("PHOTO_REPORT_HOME") or os.path.join(APP_ROOT, "storage", "projects")
    os.makedirs(d, exist_ok=True)
    return d


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def lock(pid: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(pid, threading.RLock())


def project_dir(pid: str) -> str:
    if not _ID_RX.match(pid or ""):
        raise KeyError("неверный номер проекта")
    return os.path.join(data_dir(), pid)


def _path(pid: str) -> str:
    return os.path.join(project_dir(pid), "project.json")


def load(pid: str) -> dict:
    p = _path(pid)
    if not os.path.exists(p):
        raise KeyError("проект не найден")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save(project: dict) -> dict:
    project["updated"] = _now()
    project["rev"] = int(project.get("rev", 0)) + 1          # счётчик изменений: интерфейс по нему обновляет экран
    p = _path(project["id"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(project, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return project


@contextmanager
def edit(pid: str):
    """with edit(pid) as prj: ... — загрузка, изменение и сохранение под блокировкой."""
    with lock(pid):
        prj = load(pid)
        yield prj
        save(prj)


def list_projects() -> list[dict]:
    out = []
    for name in sorted(os.listdir(data_dir()), reverse=True):
        if not _ID_RX.match(name):
            continue
        try:
            prj = load(name)
        except (KeyError, json.JSONDecodeError, OSError):
            continue
        out.append(summary(prj))
    return out


def summary(prj: dict) -> dict:
    items, photos = prj.get("items", []), prj.get("photos", [])
    by = {k: 0 for k in STATUSES}
    for it in items:
        by[it.get("status", "todo")] = by.get(it.get("status", "todo"), 0) + 1
    return {"id": prj["id"], "name": prj.get("name"), "customer": prj.get("customer"),
            "created": prj.get("created"), "updated": prj.get("updated"),
            "tz_title": (prj.get("tz") or {}).get("title", ""), "items": len(items), "by_status": by,
            "photos": len(photos), "unsorted": sum(1 for p in photos if not p.get("item_id")),
            "items_with_photos": sum(1 for it in items if it.get("photo_ids"))}


def create(name: str, customer: str = "") -> dict:
    pid = time.strftime("p%Y%m%d-") + secrets.token_hex(2)
    d = os.path.join(data_dir(), pid)
    for sub in ("tz/pages", "photos/original", "photos/work", "photos/thumb"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    prj = {"id": pid, "name": (name or "Новый проект").strip()[:200], "customer": (customer or "").strip()[:200],
           "created": _now(), "updated": _now(),
           "settings": {"enhance": "auto", "blur_faces": False, "face_mode": "blur"},
           "tz": {}, "items": [], "photos": [], "next_item": 1}
    with lock(pid):
        save(prj)
    return prj


def delete(pid: str) -> None:
    d = project_dir(pid)
    if os.path.isdir(d):
        shutil.rmtree(d)


# ------------------------------------------------------------------ пункты ТЗ
def _new_item_id(prj: dict) -> str:
    n = int(prj.get("next_item", 1))
    prj["next_item"] = n + 1
    return f"it{n}"


def new_item(prj: dict, **fields) -> dict:
    it = {"id": _new_item_id(prj), "num": "", "level": 1, "title": "", "text": "", "qty": None, "unit": "",
          "source": None, "status": "todo", "comment": "", "done_at": "", "hint": "", "photo_ids": [],
          "kind": "tz", "section": "", "required": "", "note": "", "links": []}
    it.update({k: v for k, v in fields.items() if k in it and k != "id"})
    return it


def set_tz(prj: dict, parsed: dict, rel_file: str) -> dict:
    """Пункты из разобранного ТЗ. Все пункты хранятся в tz_items (левая панель, «Все пункты ТЗ»).
    Рабочий список items — это пункты ТЗ, пока не составлен реестр подтверждений; если реестр уже есть,
    он остаётся рабочим списком и помечается устаревшим (ТЗ заменено). Привязки фото переносятся на
    пункты с тем же номером и названием; остальные фото уходят в «Неразобранное»."""
    if prj.get("registry"):
        tz_items = []
        for raw in parsed.get("items") or []:
            it = new_item(prj, **{k: raw.get(k) for k in ("num", "level", "title", "text", "qty", "unit", "source")})
            it["level"] = int(it.get("level") or 1)
            tz_items.append(it)
        prj["tz_items"] = tz_items
        prj["registry"]["stale"] = True
        prj["tz"] = _tz_meta(parsed, rel_file)
        return prj
    old = {(it.get("num"), it.get("title")): it for it in prj.get("items", [])}
    items = []
    for raw in parsed.get("items") or []:
        it = new_item(prj, **{k: raw.get(k) for k in ("num", "level", "title", "text", "qty", "unit", "source")})
        it["level"] = int(it.get("level") or 1)
        prev = old.get((it["num"], it["title"]))
        if prev:
            for k in ("status", "comment", "done_at", "hint", "photo_ids"):
                it[k] = prev.get(k, it[k])
            for p in prj.get("photos", []):
                if p.get("item_id") == prev["id"]:
                    p["item_id"] = it["id"]
        items.append(it)
    ids = {it["id"] for it in items}
    for p in prj.get("photos", []):
        if p.get("item_id") and p["item_id"] not in ids:
            p.update(item_id=None, confirmed=False)
    prj["items"] = items
    prj["tz_items"] = [dict(it, photo_ids=[]) for it in items]
    prj["tz"] = _tz_meta(parsed, rel_file)
    return prj


def _tz_meta(parsed: dict, rel_file: str) -> dict:
    return {"file": rel_file, "kind": parsed.get("kind"), "title": parsed.get("title") or "",
            "html": "tz/source.html" if parsed.get("html") else "", "pages": len(parsed.get("pages") or []),
            "warnings": list(parsed.get("warnings") or []), "full_text": "tz/full_text.txt", "loaded": _now()}


def item(prj: dict, iid: str) -> dict:
    for it in prj.get("items", []):
        if it["id"] == iid:
            return it
    raise KeyError("пункт не найден")


def photo(prj: dict, phid: str) -> dict:
    for p in prj.get("photos", []):
        if p["id"] == phid:
            return p
    raise KeyError("фото не найдено")


def assign(prj: dict, phid: str, iid: str | None, confirmed: bool = True, confidence: float | None = None,
           position: int | None = None) -> dict:
    """Положить фото в пункт (или в «Неразобранное», iid=None) и поддержать порядок item.photo_ids."""
    ph = photo(prj, phid)
    for it in prj.get("items", []):
        if phid in it.get("photo_ids", []):
            it["photo_ids"].remove(phid)
    if iid:
        it = item(prj, iid)
        ids = it.setdefault("photo_ids", [])
        if position is None or position < 0 or position > len(ids):
            ids.append(phid)
        else:
            ids.insert(position, phid)
    ph["item_id"] = iid
    ph["confirmed"] = bool(confirmed)
    if confidence is not None:
        ph["confidence"] = confidence
    return ph


def rebuild_photo_order(prj: dict) -> None:
    """Сверить item.photo_ids с photo.item_id (после массовых изменений)."""
    by_item: dict[str, list[str]] = {it["id"]: [x for x in it.get("photo_ids", [])] for it in prj.get("items", [])}
    owner = {p["id"]: p.get("item_id") for p in prj.get("photos", [])}
    for iid, ids in by_item.items():
        by_item[iid] = [x for x in ids if owner.get(x) == iid]
    for p in prj.get("photos", []):
        iid = p.get("item_id")
        if iid in by_item and p["id"] not in by_item[iid]:
            by_item[iid].append(p["id"])
    for it in prj.get("items", []):
        it["photo_ids"] = by_item.get(it["id"], [])
