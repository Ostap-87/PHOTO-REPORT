from __future__ import annotations
# -*- coding: utf-8 -*-
"""Съёмка на объекте с телефона: отдельный маленький сервер для телефона (в сети Wi-Fi или через
защищённую ссылку из интернета), а компьютер в офисе видит съёмку вживую и пишет заметки.

* Основное приложение слушает только 127.0.0.1. Телефону открывается второй сервер (0.0.0.0, порт 8020),
  на котором есть ТОЛЬКО страница съёмки и её API, и только по секретной ссылке проекта /m/<token>.
* Телефон видит пункты, которые нужно снять: что приложить, заметку офиса, свои фото и кадры,
  которые офис просит переснять. Кадр сразу проверяется на качество: тёмный или смазанный
  удаляется (по настройке) и телефон просит переснять.
* Принятые фото попадают прямо в пункт (подтверждённые) и обрабатываются как обычно.
* Из интернета — бесплатный туннель Cloudflare (cloudflared): https-ссылка без своего сервера;
  компьютер должен быть включён.
"""
import atexit
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import tarfile
import threading
import time

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from app import store

FIELD_PORT = int(os.environ.get("PHOTO_REPORT_FIELD_PORT", "8020"))
MAX_PHOTO = 40 * 1024 * 1024
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(os.path.dirname(HERE), "webui", "field.html")

field_app = FastAPI(title="ФотоОтчёт — съёмка на объекте", docs_url=None, redoc_url=None, openapi_url=None)
_state = {"server": None, "port": None, "tunnel": None, "tunnel_url": "", "tunnel_error": ""}
_lock = threading.Lock()


# ------------------------------------------------------------------ проекты и ссылки
def ensure(prj: dict) -> dict:
    f = prj.setdefault("field", {})
    if not f.get("token"):
        f["token"] = secrets.token_urlsafe(12)
    f.setdefault("auto_delete", True)
    return f


def _by_token(token: str) -> str:
    if not re.fullmatch(r"[\w-]{8,40}", token or ""):
        raise HTTPException(404, "Ссылка не найдена")
    for p in store.list_projects():
        try:
            prj = store.load(p["id"])
        except KeyError:
            continue
        if (prj.get("field") or {}).get("token") == token:
            return p["id"]
    raise HTTPException(404, "Ссылка устарела: попросите в офисе новый QR-код")


def lan_ips() -> list[str]:
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))                        # пакет не отправляется — только выбор интерфейса
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def start_server() -> int:
    """Поднять сервер для телефона (один раз на всё приложение). -> порт."""
    import uvicorn
    with _lock:
        if _state["server"] is not None:
            return _state["port"]
        port = FIELD_PORT
        for p in (FIELD_PORT, 0):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)    # как uvicorn: после перезапуска порт тот же
                try:
                    s.bind(("0.0.0.0", p))
                    port = s.getsockname()[1]
                    break
                except OSError:
                    continue
        server = uvicorn.Server(uvicorn.Config(field_app, host="0.0.0.0", port=port, log_level="warning"))
        server.install_signal_handlers = lambda: None
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(50):
            if server.started:
                break
            time.sleep(0.1)
        _state.update(server=server, port=port)
        return port


def links(pid: str) -> dict:
    prj = store.load(pid)
    token = (prj.get("field") or {}).get("token")
    port = start_server()
    urls = [f"http://{ip}:{port}/m/{token}" for ip in lan_ips()]
    out = {"urls": urls, "port": port, "auto_delete": (prj.get("field") or {}).get("auto_delete", True),
           "internet": f"{_state['tunnel_url']}/m/{token}" if _state["tunnel_url"] else "",
           "internet_error": _state["tunnel_error"]}
    out["qr"] = qr_svg(out["internet"] or (urls[0] if urls else ""))
    out["qr_internet"] = qr_svg(out["internet"]) if out["internet"] else ""
    return out


def qr_svg(text: str) -> str:
    if not text:
        return ""
    try:
        import segno
        return segno.make(text, error="m").svg_data_uri(scale=6, border=2)
    except Exception:  # noqa: BLE001 — без segno ссылку можно набрать вручную
        return ""


# ------------------------------------------------------------------ туннель из интернета (cloudflared)
def _cf_path() -> str | None:
    cands = [shutil.which("cloudflared"), "/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared",
             os.path.join(_cf_dir(), "cloudflared")]
    return next((c for c in cands if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


def _cf_dir() -> str:
    from app import secrets_store
    return secrets_store.config_dir()


def _cf_download() -> str:
    import requests
    sysname, mach = platform.system().lower(), platform.machine().lower()
    arch = "arm64" if mach in ("arm64", "aarch64") else "amd64"
    base = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    os.makedirs(_cf_dir(), exist_ok=True)
    dst = os.path.join(_cf_dir(), "cloudflared")
    if sysname == "darwin":
        r = requests.get(base + f"cloudflared-darwin-{arch}.tgz", timeout=(20, 300))
        r.raise_for_status()
        tgz = dst + ".tgz"
        with open(tgz, "wb") as fh:
            fh.write(r.content)
        with tarfile.open(tgz) as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith("cloudflared"))
            member.name = "cloudflared"
            tf.extract(member, _cf_dir())
        os.remove(tgz)
    else:
        r = requests.get(base + f"cloudflared-linux-{arch}", timeout=(20, 300))
        r.raise_for_status()
        with open(dst, "wb") as fh:
            fh.write(r.content)
    os.chmod(dst, 0o755)
    return dst


def tunnel_url(line: str) -> str:
    """Адрес туннеля из вывода cloudflared (не api.trycloudflare.com из сообщений об ошибке)."""
    m = re.search(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com", line)
    return m.group(0) if m else ""


def start_tunnel() -> str:
    """Ссылка https://….trycloudflare.com на сервер для телефона (или понятная ошибка)."""
    port = start_server()
    with _lock:
        if _state["tunnel"] is not None and _state["tunnel"].poll() is None and _state["tunnel_url"]:
            return _state["tunnel_url"]
    try:
        exe = _cf_path() or _cf_download()
    except Exception as exc:  # noqa: BLE001
        _state["tunnel_error"] = f"Не удалось скачать cloudflared ({exc}). Работает съёмка по Wi-Fi."
        raise RuntimeError(_state["tunnel_error"]) from exc
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url, t0 = "", time.time()
    while time.time() - t0 < 40:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        if "failed to request quick Tunnel" in line:            # нет связи с trycloudflare.com
            break
        url = tunnel_url(line)
        if url:
            break
    if not url:
        proc.kill()
        _state["tunnel_error"] = "Туннель Cloudflare не поднялся (сеть закрыта?). Работает съёмка по Wi-Fi."
        raise RuntimeError(_state["tunnel_error"])
    threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()   # читаем вывод дальше
    with _lock:
        _state.update(tunnel=proc, tunnel_url=url, tunnel_error="")
    atexit.register(stop_tunnel)                                # окно закрыли — туннель не остаётся висеть
    return url


def stop_tunnel() -> None:
    with _lock:
        if _state["tunnel"] is not None:
            _state["tunnel"].kill()
        _state.update(tunnel=None, tunnel_url="")


# ------------------------------------------------------------------ страница и API телефона
def _items(prj: dict) -> list[dict]:
    items = prj.get("items") or []
    out = []
    for i, it in enumerate(items):
        if it.get("status") == "na":
            continue
        has_children = i + 1 < len(items) and int(items[i + 1].get("level") or 1) > int(it.get("level") or 1)
        text = (it.get("text") or "").strip()
        if has_children and (not text or text == (it.get("title") or "").strip()) and not it.get("photo_ids"):
            continue                                            # заголовок раздела — снимать нечего
        out.append(it)
    return out


def reshoot_of(ph: dict) -> bool:
    """Как в окне офиса: решение человека важнее автоматической оценки качества."""
    r = ph.get("reshoot")
    return r if isinstance(r, bool) else bool(ph.get("reshoot_auto"))


def state(pid: str) -> dict:
    prj = store.load(pid)
    photos = {p["id"]: p for p in prj.get("photos", [])}
    rows = []
    for it in _items(prj):
        ph = [photos[i] for i in it.get("photo_ids") or [] if i in photos]
        redo = [p for p in ph if reshoot_of(p)]
        remark = (it.get("remark") or "").strip()
        last = max((p.get("field_at") or p.get("added") or "" for p in ph), default="")
        fresh_remark = bool(remark) and last <= (it.get("remark_at") or "")     # заметка не старше последнего кадра
        rows.append({"id": it["id"], "num": it.get("num") or "", "title": it.get("pf_title") or it.get("title") or "",
                     "required": it.get("required") or "", "remark": it.get("remark") or "",
                     "status": it.get("status") or "todo", "count": len(ph),
                     "need": not ph or bool(redo) or fresh_remark, "urgent": bool(redo) or fresh_remark,
                     "photos": [{"id": p["id"], "v": p.get("ver") or 0, "reshoot": reshoot_of(p),
                                 "ready": p.get("status") == "ready"} for p in ph[-8:]]})
    done = sum(1 for r in rows if r["count"] and not any(p["reshoot"] for p in r["photos"]))
    return {"project": prj.get("name") or "", "customer": prj.get("customer") or "", "rev": prj.get("rev", 0),
            "items": rows, "done": done, "total": len(rows),
            "auto_delete": (prj.get("field") or {}).get("auto_delete", True)}


@field_app.get("/m/{token}", response_class=HTMLResponse)
def page(token: str):
    _by_token(token)
    with open(PAGE, encoding="utf-8") as fh:
        return HTMLResponse(fh.read(), headers={"Cache-Control": "no-store"})


@field_app.get("/m/{token}/state")
def api_state(token: str):
    return state(_by_token(token))


@field_app.post("/m/{token}/items/{iid}/photos")
async def api_photo(token: str, iid: str, file: UploadFile = File(...)):
    """Кадр с телефона -> проверка качества -> в пункт (или «переснимите»)."""
    pid = _by_token(token)
    data = await file.read(MAX_PHOTO + 1)
    if len(data) > MAX_PHOTO:
        raise HTTPException(413, "Фото больше 40 МБ")
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(accept_photo, pid, iid, file.filename or "photo.jpg", data)   # проверка качества — не в цикле событий


def accept_photo(pid: str, iid: str, name: str, data: bytes) -> dict:
    import hashlib

    from app import pipeline
    from app.server import IMAGE_EXT, _photo_record, _safe_name
    prj = store.load(pid)
    try:
        store.item(prj, iid)
    except KeyError as exc:
        raise HTTPException(404, "Пункт не найден — обновите страницу") from exc
    name = _safe_name(name, "photo.jpg")
    ext = os.path.splitext(name)[1].lower()
    if ext not in IMAGE_EXT:
        ext, name = ".jpg", os.path.splitext(name)[0] + ".jpg"
    sha = hashlib.sha256(data).hexdigest()
    if any(p.get("sha256") == sha for p in prj.get("photos", [])):
        return {"ok": True, "duplicate": True, "message": "Это фото уже есть в пункте"}
    rec = _photo_record(name, ext, sha, len(data), iid)
    path = os.path.join(store.project_dir(pid), "photos", "original", rec["id"] + ext)
    with open(path, "wb") as fh:
        fh.write(data)
    # быстрая проверка качества: тёмный, пересвеченный или смазанный кадр не спасти улучшением
    v = pipeline._vision()
    try:
        rgb, _exif, _meta = v.io.load_image(path)
        q = v.quality.analyze(rgb)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        return {"ok": False, "reason": f"Не удалось прочитать фото: {exc}"}
    flags = q.get("flags") or []
    if q.get("reshoot"):
        why = ("слишком тёмное" if any("dark" in f for f in flags) else
               "пересвечено" if any("over" in f or "bright" in f for f in flags) else "смазано или не в фокусе")
        if (prj.get("field") or {}).get("auto_delete", True):
            os.remove(path)
            return {"ok": False, "reason": f"Фото {why} — переснимите", "deleted": True}
        rec["reshoot"] = True
    rec.update(source="field", field_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    with store.edit(pid) as prj2:
        prj2.setdefault("photos", []).append(rec)
        store.assign(prj2, rec["id"], iid, confirmed=True)
        it = store.item(prj2, iid)
        if it.get("status") == "todo":
            it["status"] = "done"
            it["done_at"] = time.strftime("%d.%m.%Y")
    pipeline.pipeline().submit(pid, [rec["id"]])
    return {"ok": True, "id": rec["id"], "warning": "Кадр слабый — офис увидит пометку «переснять»"
            if rec.get("reshoot") else ""}


@field_app.delete("/m/{token}/photos/{phid}")
def api_delete(token: str, phid: str):
    pid = _by_token(token)
    with store.edit(pid) as prj:
        try:
            ph = store.photo(prj, phid)
        except KeyError as exc:
            raise HTTPException(404, "Фото не найдено") from exc
        if ph.get("source") != "field":
            raise HTTPException(403, "С телефона можно удалить только снятые с телефона кадры")
        for it in prj.get("items", []):
            if phid in (it.get("photo_ids") or []):
                it["photo_ids"].remove(phid)
        prj["photos"] = [p for p in prj["photos"] if p["id"] != phid]
    for sub, ext in (("original", ph.get("ext") or ".jpg"), ("work", ".jpg"), ("thumb", ".jpg")):
        try:
            os.remove(os.path.join(store.project_dir(pid), "photos", sub, phid + ext))
        except OSError:
            pass
    return {"ok": True}


@field_app.post("/m/{token}/items/{iid}/status")
def api_status(token: str, iid: str, body: dict = Body(...)):
    pid = _by_token(token)
    st = body.get("status")
    if st not in ("todo", "done"):
        raise HTTPException(400, "С телефона — только «выполнено» или «не начато»")
    with store.edit(pid) as prj:
        it = store.item(prj, iid)
        it["status"] = st
        it["done_at"] = time.strftime("%d.%m.%Y") if st == "done" else ""
    return {"ok": True}


@field_app.get("/m/{token}/photos/{phid}/thumb")
def api_thumb(token: str, phid: str):
    pid = _by_token(token)
    prj = store.load(pid)
    try:
        ph = store.photo(prj, phid)
    except KeyError as exc:
        raise HTTPException(404, "Фото не найдено") from exc
    d = os.path.join(store.project_dir(pid), "photos")
    for p in (os.path.join(d, "thumb", phid + ".jpg"), os.path.join(d, "original", phid + (ph.get("ext") or ".jpg"))):
        if os.path.isfile(p):
            return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=60"})
    raise HTTPException(404, "Фото ещё обрабатывается")
