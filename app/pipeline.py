from __future__ import annotations
# -*- coding: utf-8 -*-
"""Фоновая обработка фото: чтение -> качество -> улучшение -> лица -> превью -> эмбеддинг ->
раскладка по пунктам ТЗ -> поиск дублей.

Исходник (photos/original) не меняется никогда. Рабочая копия (photos/work/<id>.jpg) —
результат улучшения и скрытия лиц по настройкам проекта (или по переопределению у фото).
Результаты копятся в памяти и записываются в project.json пачками, чтобы тысячи фото
не превращались в тысячи перезаписей файла.
"""
import hashlib
import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from app import store

DEFAULTS = {"workers": max(2, min(6, (os.cpu_count() or 4) // 2)), "threshold": None, "margin": None,
            "flush_every": 12}


def _vision():
    from app.vision import classify, dedup, enhance, faces, io, quality  # noqa: F401
    import app.vision as v
    return v


# ------------------------------------------------------------------ эмбеддинги проекта
class EmbStore:
    """emb.npy (N×D) + emb_ids.json рядом с project.json; кэш в памяти."""

    def __init__(self):
        self._cache: dict[str, tuple[list[str], np.ndarray]] = {}
        self._lock = threading.Lock()

    def _files(self, pid):
        d = store.project_dir(pid)
        return os.path.join(d, "emb.npy"), os.path.join(d, "emb_ids.json")

    def get(self, pid) -> tuple[list[str], np.ndarray]:
        with self._lock:
            if pid in self._cache:
                return self._cache[pid]
            npy, ids = self._files(pid)
            if os.path.exists(npy) and os.path.exists(ids):
                with open(ids, encoding="utf-8") as fh:
                    lst = json.load(fh)
                mat = np.load(npy)
            else:
                lst, mat = [], np.zeros((0, 0), dtype=np.float32)
            self._cache[pid] = (lst, mat)
            return lst, mat

    def put(self, pid, new: dict[str, np.ndarray]) -> None:
        if not new:
            return
        ids, mat = self.get(pid)
        with self._lock:
            ids = list(ids)
            rows = [mat[i] for i in range(len(ids))] if mat.size else []
            index = {x: i for i, x in enumerate(ids)}
            for phid, vec in new.items():
                vec = np.asarray(vec, dtype=np.float32).reshape(-1)
                if phid in index:
                    rows[index[phid]] = vec
                else:
                    index[phid] = len(ids)
                    ids.append(phid)
                    rows.append(vec)
            mat = np.vstack(rows).astype(np.float32) if rows else np.zeros((0, 0), dtype=np.float32)
            npy, idf = self._files(pid)
            np.save(npy + ".tmp.npy", mat)
            os.replace(npy + ".tmp.npy", npy)
            with open(idf + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(ids, fh)
            os.replace(idf + ".tmp", idf)
            self._cache[pid] = (ids, mat)

    def drop(self, pid, phids: set[str]) -> None:
        ids, mat = self.get(pid)
        keep = [i for i, x in enumerate(ids) if x not in phids]
        with self._lock:
            ids2 = [ids[i] for i in keep]
            mat2 = mat[keep] if mat.size else mat
            npy, idf = self._files(pid)
            np.save(npy + ".tmp.npy", mat2)
            os.replace(npy + ".tmp.npy", npy)
            with open(idf + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(ids2, fh)
            os.replace(idf + ".tmp", idf)
            self._cache[pid] = (ids2, mat2)

    def lookup(self, pid, phids: list[str]) -> np.ndarray | None:
        ids, mat = self.get(pid)
        index = {x: i for i, x in enumerate(ids)}
        rows = [index[p] for p in phids if p in index]
        if not rows or not mat.size:
            return None
        return mat[rows]


EMB = EmbStore()


# ------------------------------------------------------------------ модель
class Models:
    """Общий Embedder на всё приложение; скачивание моделей — в фоне."""

    def __init__(self):
        self._emb = None
        self._lock = threading.Lock()
        self._thread = None
        self.error = ""
        self._text_cache: dict[str, np.ndarray] = {}

    def embedder(self):
        with self._lock:
            if self._emb is None:
                from app.vision.embed import Embedder
                self._emb = Embedder()
            return self._emb

    def status(self) -> dict:
        try:
            st = dict(self.embedder().status())
        except Exception as exc:  # noqa: BLE001
            st = {"ready": False, "downloading": False, "progress": 0, "error": str(exc)}
        if self.error and not st.get("error"):
            st["error"] = self.error
        return st

    def ensure_async(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def work():
            try:
                self.embedder().ensure()
                self.error = ""
            except Exception as exc:  # noqa: BLE001
                self.error = f"модель не загружена: {exc}"
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def ready(self) -> bool:
        try:
            return bool(self.embedder().status().get("ready"))
        except Exception:  # noqa: BLE001
            return False

    def text_embs(self, prompts: list[str]) -> np.ndarray:
        key = hashlib.sha1("␟".join(prompts).encode("utf-8")).hexdigest()
        if key not in self._text_cache:
            self._text_cache[key] = self.embedder().encode_texts(prompts)
        return self._text_cache[key]


MODELS = Models()


# ------------------------------------------------------------------ обработка
class Job:
    def __init__(self, pid):
        self.pid = pid
        self.total = 0
        self.done = 0
        self.errors = 0
        self.stage = "idle"
        self.started = 0.0
        self.message = ""
        self.pending: dict[str, dict] = {}
        self.pending_emb: dict[str, np.ndarray] = {}
        self.lock = threading.Lock()
        self.active = 0

    def as_dict(self) -> dict:
        return {"total": self.total, "done": self.done, "errors": self.errors, "stage": self.stage,
                "message": self.message, "running": self.active > 0 or self.stage in ("processing", "classifying"),
                "elapsed": round(time.time() - self.started, 1) if self.started else 0}


class Pipeline:
    def __init__(self, workers: int | None = None):
        self.pool = ThreadPoolExecutor(max_workers=workers or DEFAULTS["workers"], thread_name_prefix="photo")
        self.jobs: dict[str, Job] = {}
        self._jobs_lock = threading.Lock()

    def job(self, pid) -> Job:
        with self._jobs_lock:
            return self.jobs.setdefault(pid, Job(pid))

    def status(self, pid) -> dict:
        return self.job(pid).as_dict()

    # -------------------------------------------------------------- постановка в очередь
    def submit(self, pid: str, phids: list[str], reprocess: bool = False) -> None:
        if not phids:
            return
        job = self.job(pid)
        with job.lock:
            if job.active == 0:
                job.total, job.done, job.errors, job.started = 0, 0, 0, time.time()
            job.total += len(phids)
            job.active += len(phids)
            job.stage = "processing"
            job.message = "обработка фото"
        for phid in phids:
            self.pool.submit(self._run_one, pid, phid, reprocess)

    def _run_one(self, pid: str, phid: str, reprocess: bool) -> None:
        job = self.job(pid)
        try:
            fields, emb = process_photo(pid, phid, reprocess=reprocess)
            with job.lock:
                job.pending.setdefault(phid, {}).update(fields)
                if emb is not None:
                    job.pending_emb[phid] = emb
                job.done += 1
        except Exception as exc:  # noqa: BLE001
            with job.lock:
                job.pending.setdefault(phid, {}).update(status="error", error=str(exc)[:300])
                job.errors += 1
                job.done += 1
            traceback.print_exc()
        finally:
            with job.lock:
                job.active -= 1
                last = job.active == 0
                need_flush = last or len(job.pending) >= DEFAULTS["flush_every"]
            if need_flush:
                self.flush(pid)
            if last:
                self._after_batch(pid)

    def flush(self, pid: str) -> None:
        job = self.job(pid)
        with job.lock:
            pend, job.pending = job.pending, {}
            pemb, job.pending_emb = job.pending_emb, {}
        if pemb:
            EMB.put(pid, pemb)
        if not pend:
            return
        with store.edit(pid) as prj:
            for p in prj.get("photos", []):
                if p["id"] in pend:
                    p.update(pend[p["id"]])

    def _after_batch(self, pid: str) -> None:
        job = self.job(pid)
        try:
            job.stage, job.message = "classifying", "раскладка по пунктам ТЗ"
            classify_project(pid)
            mark_duplicates(pid)
            job.stage, job.message = "idle", "готово"
        except Exception as exc:  # noqa: BLE001
            job.stage, job.message = "idle", f"раскладка не выполнена: {exc}"
            traceback.print_exc()


PIPE: Pipeline | None = None


def pipeline() -> Pipeline:
    global PIPE
    if PIPE is None:
        PIPE = Pipeline()
    return PIPE


# ------------------------------------------------------------------ одно фото
def _paths(pid: str, ph: dict) -> dict:
    d = store.project_dir(pid)
    return {"original": os.path.join(d, "photos", "original", ph["id"] + ph.get("ext", ".jpg")),
            "work": os.path.join(d, "photos", "work", ph["id"] + ".jpg"),
            "thumb": os.path.join(d, "photos", "thumb", ph["id"] + ".jpg")}


def process_photo(pid: str, phid: str, reprocess: bool = False) -> tuple[dict, np.ndarray | None]:
    """Обработать одно фото по настройкам проекта. -> (поля для project.json, эмбеддинг или None)."""
    v = _vision()
    prj = store.load(pid)
    ph = store.photo(prj, phid)
    st = prj.get("settings") or {}
    paths = _paths(pid, ph)
    rgb, exif_bytes, meta = v.io.load_image(paths["original"])
    q = v.quality.analyze(rgb)
    mode = ph.get("enhance_override") or st.get("enhance", "auto")
    work, applied = v.enhance.enhance(rgb, q, mode=mode) if mode != "off" else (rgb, [])
    blur_faces = st.get("blur_faces", False) if ph.get("faces_override") is None else bool(ph["faces_override"])
    boxes = v.faces.detect(work)
    if blur_faces and boxes:
        work = v.faces.blur(work, boxes, mode=st.get("face_mode", "blur"))
    v.io.save_jpeg(work, paths["work"], exif_bytes=exif_bytes, quality=92)
    v.io.save_jpeg(v.io.thumbnail(work, 480), paths["thumb"], exif_bytes=None, quality=82)
    fields = {"status": "ready", "error": "", "w": int(meta.get("w") or rgb.shape[1]), "h": int(meta.get("h") or rgb.shape[0]),
              "exif": {k: meta.get(k) for k in ("datetime", "gps", "camera")},
              "quality": _jsonable(q), "enhanced": bool(applied), "applied": list(applied),
              "faces": len(boxes), "faces_blurred": bool(blur_faces and boxes),
              "dhash": format(int(v.dedup.dhash(rgb)), "016x"), "reshoot_auto": bool(q.get("reshoot")),
              "ver": int(time.time() * 1000) % 1_000_000_000}
    emb = None
    if MODELS.ready() and EMB.lookup(pid, [phid]) is None:
        # эмбеддинг считается один раз по исходнику: от настроек улучшения и лиц он не зависит
        emb = MODELS.embedder().encode_images([rgb])[0]
    return fields, emb


def _jsonable(d):
    if isinstance(d, dict):
        return {k: _jsonable(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [_jsonable(x) for x in d]
    if isinstance(d, (np.floating,)):
        return round(float(d), 4)
    if isinstance(d, (np.integer,)):
        return int(d)
    if isinstance(d, (np.bool_,)):
        return bool(d)
    if isinstance(d, float):
        return round(d, 4)
    return d


# ------------------------------------------------------------------ раскладка и дубли
def classify_project(pid: str, params: dict | None = None) -> dict:
    """Разложить неподтверждённые фото по пунктам ТЗ. Подтверждённые человеком не трогаются
    и служат примерами. -> {"classified": n, "assigned": k, "unsorted": m} или причина пропуска."""
    v = _vision()
    prj = store.load(pid)
    items = [it for it in prj.get("items", []) if it.get("status") != "na"]
    if not items:
        return {"skipped": "нет пунктов ТЗ"}
    if not MODELS.ready():
        return {"skipped": "модель распознавания ещё не загружена"}
    # эмбеддинги для фото, которые обработаны до готовности модели
    missing = [p for p in prj.get("photos", []) if p.get("status") == "ready" and EMB.lookup(pid, [p["id"]]) is None]
    if missing:
        new = {}
        for p in missing:
            rgb, _, _ = v.io.load_image(_paths(pid, p)["original"])
            new[p["id"]] = MODELS.embedder().encode_images([rgb])[0]
        EMB.put(pid, new)
    item_text = {it["id"]: MODELS.text_embs(v.classify.build_prompts(it)) for it in items}
    examples = {}
    for it in items:
        conf = [p["id"] for p in prj.get("photos", []) if p.get("item_id") == it["id"] and p.get("confirmed")]
        e = EMB.lookup(pid, conf) if conf else None
        if e is not None:
            examples[it["id"]] = e
    todo = [p["id"] for p in prj.get("photos", []) if p.get("status") == "ready" and not p.get("confirmed")]
    embs = EMB.lookup(pid, todo)
    if embs is None:
        return {"classified": 0}
    todo = [x for x in todo if EMB.lookup(pid, [x]) is not None]
    prm = {k: v_ for k, v_ in (params or {}).items() if v_ is not None}
    res = v.classify.classify(embs, item_text, examples, prm)
    assigned = 0
    with store.edit(pid) as prj2:
        for phid, r in zip(todo, res):
            try:
                ph = store.photo(prj2, phid)
            except KeyError:
                continue
            if ph.get("confirmed"):
                continue                       # человек успел положить сам — не трогаем
            store.assign(prj2, phid, r.get("item_id"), confirmed=False, confidence=float(r.get("confidence") or 0))
            ph["suggestions"] = [[a, round(float(b), 4)] for a, b in (r.get("suggestions") or [])]
            assigned += 1 if r.get("item_id") else 0
    return {"classified": len(todo), "assigned": assigned, "unsorted": len(todo) - assigned}


def mark_duplicates(pid: str, max_dist: int = 6) -> int:
    v = _vision()
    with store.edit(pid) as prj:
        photos = [p for p in prj.get("photos", []) if p.get("dhash")]
        hashes = [int(p["dhash"], 16) for p in photos]
        for p in prj.get("photos", []):
            p["duplicate_of"] = None
        n = 0
        for grp in v.dedup.groups(hashes, max_dist=max_dist):
            if len(grp) < 2:
                continue
            blur = [float((photos[i].get("quality") or {}).get("blur") or 0) for i in grp]
            best = v.dedup.best(grp, blur) if hasattr(v.dedup, "best") else grp[int(np.argmax(blur))]
            for i in grp:
                if i != best:
                    photos[i]["duplicate_of"] = photos[best]["id"]
                    n += 1
    return n
