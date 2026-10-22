from __future__ import annotations
# -*- coding: utf-8 -*-
"""HTTP-сервер ФотоОтчёта: проекты, ТЗ, фото, раскладка, отчёты. Только локально (127.0.0.1).

Запуск: python -m uvicorn app.server:app --port 8010   (или photo_gui.py — окно на Mac/Windows)
"""
import hashlib
import io
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

import yaml
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from app import pipeline, store

APP_ROOT = store.APP_ROOT
WEBUI = os.path.join(APP_ROOT, "webui")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp"}
TZ_EXT = {".docx", ".pdf", ".xlsx", ".xlsm", ".csv", ".txt", ".md"}
DONE_STATUSES = {"done", "provided", "executed"}


def settings() -> dict:
    path = os.path.join(APP_ROOT, "config", "settings.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except OSError:
        return {}


app = FastAPI(title="ФотоОтчёт", docs_url=None, redoc_url=None)


@app.exception_handler(KeyError)
def _not_found(_request, exc: KeyError):
    return JSONResponse({"detail": str(exc.args[0] if exc.args else "не найдено")}, status_code=404)


@app.on_event("startup")
def _startup():
    st = settings()
    cls = st.get("classify") or {}
    pipeline.DEFAULTS.update({k: cls[k] for k in ("threshold", "margin") if k in cls})
    if st.get("auto_download_models", True) and not os.environ.get("PHOTO_REPORT_NO_DOWNLOAD"):
        pipeline.MODELS.ensure_async()


# ------------------------------------------------------------------ помощники
def _load(pid: str) -> dict:
    try:
        return store.load(pid)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


def _safe_name(name: str, default: str = "file") -> str:
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", base)
    return base[:180] or default


def _full(prj: dict) -> dict:
    out = dict(prj)
    out["summary"] = store.summary(prj)
    out["job"] = pipeline.pipeline().status(prj["id"])
    out["statuses"] = store.STATUSES
    return out


def _params() -> dict:
    return {k: pipeline.DEFAULTS.get(k) for k in ("threshold", "margin") if pipeline.DEFAULTS.get(k) is not None}


def _classify_bg(pid: str) -> None:
    def work():
        job = pipeline.pipeline().job(pid)
        try:
            job.stage, job.message = "classifying", "раскладка по пунктам ТЗ"
            r = pipeline.classify_project(pid, _params())
            pipeline.mark_duplicates(pid)
            job.message = r.get("skipped") or "готово"
        except Exception as exc:  # noqa: BLE001
            job.message = f"раскладка не выполнена: {exc}"
        finally:
            job.stage = "idle"
    threading.Thread(target=work, daemon=True).start()


# ------------------------------------------------------------------ страница и служебное
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(WEBUI, "index.html"), encoding="utf-8") as fh:
        return HTMLResponse(fh.read(), headers={"Cache-Control": "no-store"})


@app.get("/favicon.svg")
def favicon():
    return FileResponse(os.path.join(WEBUI, "favicon.svg"), media_type="image/svg+xml")


@app.get("/api/health")
def health():
    return {"ok": True, "data_dir": store.data_dir()}


@app.get("/api/model")
def model_status():
    return pipeline.MODELS.status()


@app.post("/api/model/download")
def model_download():
    pipeline.MODELS.ensure_async()
    return pipeline.MODELS.status()


# ------------------------------------------------------------------ проекты
@app.get("/api/projects")
def projects():
    return {"projects": store.list_projects(), "data_dir": store.data_dir()}


@app.post("/api/projects")
def project_create(body: dict = Body(...)):
    prj = store.create(body.get("name") or "", body.get("customer") or "")
    return _full(prj)


@app.get("/api/projects/{pid}")
def project_get(pid: str):
    return _full(_load(pid))


@app.patch("/api/projects/{pid}")
def project_patch(pid: str, body: dict = Body(...)):
    _load(pid)
    reprocess = False
    with store.edit(pid) as prj:
        for k in ("name", "customer"):
            if k in body:
                prj[k] = str(body[k] or "").strip()[:200]
        if isinstance(body.get("settings"), dict):
            new = body["settings"]
            cur = prj.setdefault("settings", {})
            if new.get("enhance") in ("auto", "all", "off") and new["enhance"] != cur.get("enhance"):
                cur["enhance"], reprocess = new["enhance"], True
            if "blur_faces" in new and bool(new["blur_faces"]) != bool(cur.get("blur_faces")):
                cur["blur_faces"], reprocess = bool(new["blur_faces"]), True
            if new.get("face_mode") in ("blur", "pixelate") and new["face_mode"] != cur.get("face_mode"):
                cur["face_mode"] = new["face_mode"]
                reprocess = reprocess or bool(cur.get("blur_faces"))
        ids = [p["id"] for p in prj.get("photos", []) if p.get("status") in ("ready", "error")]
    if reprocess and ids and body.get("apply_to_existing", True):
        pipeline.pipeline().submit(pid, ids, reprocess=True)
    return _full(_load(pid))


@app.delete("/api/projects/{pid}")
def project_delete(pid: str):
    _load(pid)
    store.delete(pid)
    return {"ok": True}


# ------------------------------------------------------------------ ТЗ
@app.post("/api/projects/{pid}/tz")
async def tz_upload(pid: str, file: UploadFile = File(...)):
    _load(pid)
    name = _safe_name(file.filename, "ТЗ")
    ext = os.path.splitext(name)[1].lower()
    if ext not in TZ_EXT:
        raise HTTPException(400, "ТЗ принимается в форматах Word (.docx), PDF, Excel (.xlsx), CSV или текст. "
                                 "Старые .doc и .xls пересохраните в новом формате.")
    data = await file.read()
    from app.tz import parse_tz
    try:
        parsed = parse_tz(data, name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Не удалось разобрать ТЗ: {exc}") from exc
    if not parsed.get("items"):
        raise HTTPException(400, "В файле не найдено ни одного пункта. Проверьте, что это ТЗ с нумерованными "
                                 "пунктами или таблицей, либо добавьте пункты вручную.")
    d = store.project_dir(pid)
    tzdir = os.path.join(d, "tz")
    for old in os.listdir(tzdir):
        p = os.path.join(tzdir, old)
        if os.path.isfile(p):
            os.remove(p)
    shutil.rmtree(os.path.join(tzdir, "pages"), ignore_errors=True)
    os.makedirs(os.path.join(tzdir, "pages"), exist_ok=True)
    with open(os.path.join(tzdir, name), "wb") as fh:
        fh.write(data)
    if parsed.get("html"):
        with open(os.path.join(tzdir, "source.html"), "w", encoding="utf-8") as fh:
            fh.write(parsed["html"])
    for i, png in enumerate(parsed.get("pages") or [], 1):
        with open(os.path.join(tzdir, "pages", f"page-{i:03d}.png"), "wb") as fh:
            fh.write(png)
    with store.edit(pid) as prj:
        store.set_tz(prj, parsed, f"tz/{name}")
        has_photos = any(p.get("status") == "ready" for p in prj.get("photos", []))
    if has_photos:
        _classify_bg(pid)
    return _full(_load(pid))


@app.get("/api/projects/{pid}/tz/html", response_class=HTMLResponse)
def tz_html(pid: str):
    p = os.path.join(store.project_dir(pid), "tz", "source.html")
    if not os.path.exists(p):
        raise HTTPException(404, "нет HTML-копии ТЗ")
    with open(p, encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


@app.get("/api/projects/{pid}/tz/pages/{n}.png")
def tz_page(pid: str, n: int):
    p = os.path.join(store.project_dir(pid), "tz", "pages", f"page-{n:03d}.png")
    if not os.path.exists(p):
        raise HTTPException(404, "нет страницы")
    return FileResponse(p, media_type="image/png")


@app.get("/api/projects/{pid}/tz/file")
def tz_file(pid: str):
    prj = _load(pid)
    rel = (prj.get("tz") or {}).get("file")
    if not rel:
        raise HTTPException(404, "ТЗ не загружено")
    p = os.path.join(store.project_dir(pid), rel)
    return FileResponse(p, filename=os.path.basename(p))


# ------------------------------------------------------------------ пункты
ITEM_FIELDS = {"title", "text", "qty", "unit", "comment", "hint", "num", "level", "done_at"}


@app.post("/api/projects/{pid}/items")
def item_add(pid: str, body: dict = Body(...)):
    _load(pid)
    with store.edit(pid) as prj:
        it = store.new_item(prj, **{k: body.get(k) for k in ITEM_FIELDS if body.get(k) is not None})
        it["title"] = (it["title"] or "Новый пункт").strip()[:300]
        after = body.get("after")
        items = prj.setdefault("items", [])
        pos = next((i + 1 for i, x in enumerate(items) if x["id"] == after), len(items))
        items.insert(pos, it)
    return {"item": it, "project": _full(_load(pid))}


@app.patch("/api/projects/{pid}/items/{iid}")
def item_patch(pid: str, iid: str, body: dict = Body(...)):
    _load(pid)
    reclassify = False
    with store.edit(pid) as prj:
        try:
            it = store.item(prj, iid)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        for k in ITEM_FIELDS:
            if k in body:
                v = body[k]
                if k == "level":
                    v = max(1, min(6, int(v or 1)))
                elif k == "qty":
                    v = None if v in ("", None) else v
                else:
                    v = "" if v is None else str(v)[:5000]
                if k in ("hint", "title", "text") and v != it.get(k):
                    reclassify = True
                it[k] = v
        if "status" in body:
            st = body["status"]
            if st not in store.STATUSES:
                raise HTTPException(400, "неизвестный статус")
            it["status"] = st
            if st in DONE_STATUSES and not it.get("done_at"):
                it["done_at"] = time.strftime("%d.%m.%Y")
            if st == "todo":
                it["done_at"] = ""
        if isinstance(body.get("photo_ids"), list):
            mine = {p["id"] for p in prj.get("photos", []) if p.get("item_id") == iid}
            order = [x for x in body["photo_ids"] if x in mine]
            it["photo_ids"] = order + [x for x in it.get("photo_ids", []) if x not in order and x in mine]
        out = dict(it)
    if reclassify and body.get("reclassify", True):
        _classify_bg(pid)
    return out


@app.delete("/api/projects/{pid}/items/{iid}")
def item_delete(pid: str, iid: str):
    _load(pid)
    with store.edit(pid) as prj:
        prj["items"] = [x for x in prj.get("items", []) if x["id"] != iid]
        for p in prj.get("photos", []):
            if p.get("item_id") == iid:
                p.update(item_id=None, confirmed=False, confidence=None)
    return _full(_load(pid))


# ------------------------------------------------------------------ фото
def _photo_record(name: str, ext: str, sha: str, size: int, item_id: str | None) -> dict:
    return {"id": "ph_" + secrets.token_hex(4), "name": name, "ext": ext, "sha256": sha, "size": size,
            "added": time.strftime("%Y-%m-%d %H:%M:%S"), "status": "queued", "error": "",
            "item_id": item_id, "confirmed": bool(item_id), "confidence": None, "suggestions": [],
            "duplicate_of": None}


def _iter_upload_files(name: str, data: bytes):
    """(имя, байты) картинок: сам файл или содержимое ZIP-архива."""
    ext = os.path.splitext(name)[1].lower()
    if ext == ".zip":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise HTTPException(400, f"Архив {name} повреждён") from exc
        for info in zf.infolist():
            if info.is_dir() or "__MACOSX" in info.filename or os.path.basename(info.filename).startswith("._"):
                continue
            n = _safe_name(info.filename)
            if os.path.splitext(n)[1].lower() in IMAGE_EXT:
                yield n, zf.read(info)
    elif ext in IMAGE_EXT:
        yield name, data


@app.post("/api/projects/{pid}/photos")
async def photos_upload(pid: str, files: list[UploadFile] = File(...), item_id: str = Form("")):
    prj = _load(pid)
    if item_id:
        try:
            store.item(prj, item_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
    known = {p.get("sha256"): p["id"] for p in prj.get("photos", [])}
    d = store.project_dir(pid)
    new, skipped, rejected = [], [], []
    for f in files:
        data = await f.read()
        fname = _safe_name(f.filename, "photo.jpg")
        found = False
        for name, blob in _iter_upload_files(fname, data):
            found = True
            sha = hashlib.sha256(blob).hexdigest()
            if sha in known:
                skipped.append(name)
                continue
            ext = os.path.splitext(name)[1].lower()
            rec = _photo_record(name, ext, sha, len(blob), item_id or None)
            with open(os.path.join(d, "photos", "original", rec["id"] + ext), "wb") as fh:
                fh.write(blob)
            known[sha] = rec["id"]
            new.append(rec)
        if not found:
            rejected.append(fname)
    if new:
        with store.edit(pid) as prj2:
            prj2.setdefault("photos", []).extend(new)
            if item_id:
                for rec in new:
                    store.assign(prj2, rec["id"], item_id, confirmed=True)
        pipeline.pipeline().submit(pid, [r["id"] for r in new])
    return {"added": len(new), "skipped_duplicates": skipped, "rejected": rejected,
            "job": pipeline.pipeline().status(pid)}


def _photo_path(pid: str, ph: dict, kind: str) -> str:
    d = store.project_dir(pid)
    if kind == "original":
        return os.path.join(d, "photos", "original", ph["id"] + ph.get("ext", ".jpg"))
    return os.path.join(d, "photos", kind, ph["id"] + ".jpg")


def _jpeg_preview(path: str, max_side: int) -> bytes:
    from app.vision import io as vio
    import cv2
    rgb, _, _ = vio.load_image(path)
    small = vio.thumbnail(rgb, max_side)
    ok, buf = cv2.imencode(".jpg", small[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise HTTPException(500, "не удалось подготовить превью")
    return buf.tobytes()


@app.get("/api/projects/{pid}/photos/{phid}/{kind}")
def photo_file(pid: str, phid: str, kind: str):
    prj = _load(pid)
    try:
        ph = store.photo(prj, phid)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    cache = {"Cache-Control": "no-cache"}
    if kind in ("thumb", "work"):
        p = _photo_path(pid, ph, kind)
        if not os.path.exists(p):
            raise HTTPException(404, "фото ещё обрабатывается")
        return FileResponse(p, media_type="image/jpeg", headers=cache)
    if kind == "original":
        p = _photo_path(pid, ph, "original")
        return FileResponse(p, filename=ph.get("name") or os.path.basename(p))
    if kind in ("view", "before"):           # для просмотра «до / после»: JPEG до 1600 px
        src = _photo_path(pid, ph, "work" if kind == "view" else "original")
        if not os.path.exists(src):
            raise HTTPException(404, "фото ещё обрабатывается")
        return Response(_jpeg_preview(src, 1600), media_type="image/jpeg", headers=cache)
    raise HTTPException(404, "неизвестный вид файла")


@app.patch("/api/projects/{pid}/photos/{phid}")
def photo_patch(pid: str, phid: str, body: dict = Body(...)):
    _load(pid)
    reprocess = False
    with store.edit(pid) as prj:
        try:
            ph = store.photo(prj, phid)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if "item_id" in body:
            iid = body["item_id"] or None
            try:
                store.assign(prj, phid, iid, confirmed=True, position=body.get("position"))
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
        if body.get("confirm"):
            ph["confirmed"] = True
        if "enhance_override" in body:
            v = body["enhance_override"]
            ph["enhance_override"] = v if v in ("auto", "all", "off") else None
            reprocess = True
        if "faces_override" in body:
            v = body["faces_override"]
            ph["faces_override"] = None if v is None else bool(v)
            reprocess = True
        if "reshoot" in body:
            ph["reshoot"] = bool(body["reshoot"])
        if "note" in body:
            ph["note"] = str(body["note"] or "")[:1000]
        out = dict(ph)
    if reprocess:
        pipeline.pipeline().submit(pid, [phid], reprocess=True)
    return out


@app.post("/api/projects/{pid}/photos/bulk")
def photos_bulk(pid: str, body: dict = Body(...)):
    _load(pid)
    ids = [x for x in (body.get("photo_ids") or []) if isinstance(x, str)]
    action = body.get("action")
    if not ids:
        raise HTTPException(400, "не выбраны фото")
    if action == "assign":
        iid = body.get("item_id") or None
        with store.edit(pid) as prj:
            for phid in ids:
                try:
                    store.assign(prj, phid, iid, confirmed=True)
                except KeyError:
                    continue
    elif action == "confirm":
        with store.edit(pid) as prj:
            for p in prj.get("photos", []):
                if p["id"] in ids and p.get("item_id"):
                    p["confirmed"] = True
    elif action == "delete":
        _delete_photos(pid, set(ids))
    elif action == "reprocess":
        pipeline.pipeline().submit(pid, ids, reprocess=True)
    else:
        raise HTTPException(400, "неизвестное действие")
    return _full(_load(pid))


def _delete_photos(pid: str, ids: set[str]) -> None:
    with store.edit(pid) as prj:
        gone = [p for p in prj.get("photos", []) if p["id"] in ids]
        prj["photos"] = [p for p in prj.get("photos", []) if p["id"] not in ids]
        for it in prj.get("items", []):
            it["photo_ids"] = [x for x in it.get("photo_ids", []) if x not in ids]
        for p in prj["photos"]:
            if p.get("duplicate_of") in ids:
                p["duplicate_of"] = None
    for p in gone:
        for kind in ("original", "work", "thumb"):
            try:
                os.remove(_photo_path(pid, p, kind))
            except OSError:
                pass
    pipeline.EMB.drop(pid, ids)


@app.delete("/api/projects/{pid}/photos/{phid}")
def photo_delete(pid: str, phid: str):
    _load(pid)
    _delete_photos(pid, {phid})
    return {"ok": True}


@app.post("/api/projects/{pid}/classify")
def classify(pid: str, body: dict = Body(default={})):
    prj = _load(pid)
    if body.get("reset"):
        with store.edit(pid) as prj2:
            for p in prj2.get("photos", []):
                if not p.get("confirmed"):
                    store.assign(prj2, p["id"], None, confirmed=False)
    if not pipeline.MODELS.ready():
        pipeline.MODELS.ensure_async()
        return JSONResponse({"skipped": "модель распознавания загружается, раскладка начнётся после загрузки",
                             "model": pipeline.MODELS.status()}, status_code=202)
    if not prj.get("items"):
        raise HTTPException(400, "сначала загрузите ТЗ или добавьте пункты")
    _classify_bg(pid)
    return {"started": True}


@app.post("/api/projects/{pid}/reprocess")
def reprocess(pid: str):
    prj = _load(pid)
    ids = [p["id"] for p in prj.get("photos", [])]
    pipeline.pipeline().submit(pid, ids, reprocess=True)
    return pipeline.pipeline().status(pid)


@app.get("/api/projects/{pid}/status")
def status(pid: str):
    prj = _load(pid)
    return {"job": pipeline.pipeline().status(pid), "summary": store.summary(prj), "model": pipeline.MODELS.status(),
            "updated": prj.get("updated"), "rev": prj.get("rev", 0)}


# ------------------------------------------------------------------ отчёты
def _report_name(prj: dict, ext: str) -> str:
    base = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", prj.get("name") or "Фотоотчёт").strip()[:80] or "Фотоотчёт"
    return f"{base} — {time.strftime('%Y-%m-%d')}.{ext}"


@app.get("/api/projects/{pid}/export/{fmt}")
def export(pid: str, fmt: str, which: str = Query("work"), photos_per_row: int = Query(2),
           include_unsorted: bool = Query(False), only_done: bool = Query(False)):
    prj = _load(pid)
    from app import export as ex
    d = store.project_dir(pid)
    outdir = os.path.join(d, "exports")
    os.makedirs(outdir, exist_ok=True)
    options = {"photos_per_row": max(1, min(4, photos_per_row)), "include_unsorted": include_unsorted,
               "only_status": sorted(DONE_STATUSES) if only_done else None}
    name = _report_name(prj, fmt)
    out = os.path.join(outdir, f"report.{fmt}")
    try:
        if fmt == "docx":
            ex.export_docx(prj, d, out, options)
        elif fmt == "pdf":
            ex.export_pdf(prj, d, out, options)
        elif fmt == "xlsx":
            ex.export_xlsx(prj, d, out)
        elif fmt == "zip":
            ex.export_zip(prj, d, out, "original" if which == "original" else "work")
        else:
            raise HTTPException(404, "неизвестный формат")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Отчёт не собран: {exc}") from exc
    return FileResponse(out, filename=name)


@app.post("/api/projects/{pid}/export/folders")
def export_folders(pid: str, body: dict = Body(default={})):
    """Разложить фото по настоящим папкам на диске: <папка выгрузки>/<проект>/<№ пункт>/…"""
    prj = _load(pid)
    from app import export as ex
    target_root = body.get("target") or os.environ.get("PHOTO_REPORT_EXPORT") or os.path.join(
        os.path.dirname(store.data_dir().rstrip(os.sep)), "Папки")
    base = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", prj.get("name") or prj["id"]).strip()[:80] or prj["id"]
    dest = os.path.join(target_root, base)
    with tempfile.TemporaryDirectory() as tmp:
        zpath = os.path.join(tmp, "f.zip")
        ex.export_zip(prj, store.project_dir(pid), zpath, "original" if body.get("which") == "original" else "work")
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(zpath) as zf:
            for info in zf.infolist():
                parts = [p for p in info.filename.replace("\\", "/").split("/") if p not in ("", ".", "..")]
                if not parts or info.is_dir():
                    continue
                out = os.path.join(dest, *parts)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with zf.open(info) as src, open(out, "wb") as fh:
                    shutil.copyfileobj(src, fh)
    if body.get("open", True) and sys.platform == "darwin":
        subprocess.Popen(["open", dest])
    elif body.get("open", True) and sys.platform.startswith("win"):
        os.startfile(dest)  # type: ignore[attr-defined]  # noqa: S606
    return {"path": dest, "folders": sorted(x for x in os.listdir(dest) if os.path.isdir(os.path.join(dest, x)))}
