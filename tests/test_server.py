# -*- coding: utf-8 -*-
"""Сервер и хранилище: проекты, пункты, загрузка фото, раскладка, отчёты. Модель распознавания
подменяется детерминированной заглушкой, чтобы тесты шли без скачивания и без сети."""
from __future__ import annotations

import io
import time
import zipfile

import numpy as np
import pytest
from PIL import Image


def _jpeg(color, size=(320, 240), dark=False) -> bytes:
    arr = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    arr[:] = color
    arr[size[1] // 3: size[1] // 2, size[0] // 4: size[0] // 2] = (255 - np.array(color)) // 2
    if dark:
        arr = (arr * 0.15).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=90)
    return buf.getvalue()


class FakeEmbedder:
    """Цвет фото -> «смысл»: красный ≈ столы, синий ≈ сцена. Тексты — по ключевым словам."""

    def status(self):
        return {"ready": True, "downloading": False, "progress": 1.0, "error": "", "size_mb": 0}

    def ensure(self, progress_cb=None):
        return None

    def encode_images(self, images):
        out = []
        for rgb in images:
            m = rgb.reshape(-1, 3).mean(0) + 1
            v = np.array([m[0], m[2], m[1] * 0.3, 1.0], dtype=np.float32)
            out.append(v / np.linalg.norm(v))
        return np.vstack(out)

    def encode_texts(self, texts):
        out = []
        for t in texts:
            t = t.lower()
            v = np.array([5.0 if "стол" in t else 0.2, 5.0 if "сцен" in t else 0.2, 0.3, 0.6], dtype=np.float32)
            out.append(v / np.linalg.norm(v))
        return np.vstack(out)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_REPORT_HOME", str(tmp_path / "projects"))
    monkeypatch.setenv("PHOTO_REPORT_NO_DOWNLOAD", "1")
    from fastapi.testclient import TestClient
    from app import pipeline
    import app.server as server
    pipeline.MODELS._emb = FakeEmbedder()
    pipeline.MODELS._text_cache.clear()
    pipeline.EMB._cache.clear()
    pipeline.PIPE = pipeline.Pipeline(workers=2)
    with TestClient(server.app) as c:
        yield c
    pipeline.MODELS._emb = None


def _wait(c, pid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = c.get(f"/api/projects/{pid}/status").json()
        if not st["job"]["running"]:
            return st
        time.sleep(0.2)
    raise AssertionError("обработка не закончилась")


TZ_TXT = """Техническое задание на оформление банкета

1. Расстановка столов
1.1 Банкетные столы с белыми скатертями — 12 шт.
2. Сцена
2.1 Сцена с подсветкой и экраном
3. Уборка после мероприятия
"""


def _project_with_tz(c):
    pid = c.post("/api/projects", json={"name": "Банкет", "customer": "ООО «Пример»"}).json()["id"]
    r = c.post(f"/api/projects/{pid}/tz", files={"file": ("ТЗ.txt", TZ_TXT.encode("utf-8"))})
    assert r.status_code == 200, r.text
    return pid, r.json()


def test_tz_items_become_folders(client):
    pid, prj = _project_with_tz(client)
    titles = [i["title"] for i in prj["items"]]
    assert any("стол" in t.lower() for t in titles) and any("сцен" in t.lower() for t in titles)
    assert all(i["status"] == "todo" and i["photo_ids"] == [] for i in prj["items"])
    assert client.get(f"/api/projects/{pid}/tz/file").status_code == 200


def test_status_comment_and_date(client):
    pid, prj = _project_with_tz(client)
    iid = prj["items"][0]["id"]
    out = client.patch(f"/api/projects/{pid}/items/{iid}", json={"status": "provided", "comment": "готово"}).json()
    assert out["status"] == "provided" and out["done_at"] and out["comment"] == "готово"
    out = client.patch(f"/api/projects/{pid}/items/{iid}", json={"status": "todo"}).json()
    assert out["done_at"] == ""
    assert client.patch(f"/api/projects/{pid}/items/{iid}", json={"status": "лишний"}).status_code == 400


def test_upload_process_classify_and_confirm(client):
    pid, prj = _project_with_tz(client)
    files = [("files", (f"red{i}.jpg", _jpeg((200 + i, 30, 30)))) for i in range(3)]
    files += [("files", (f"blue{i}.jpg", _jpeg((30, 30, 200 + i)))) for i in range(3)]
    files.append(("files", ("dark.jpg", _jpeg((180, 40, 40), dark=True))))
    r = client.post(f"/api/projects/{pid}/photos", files=files).json()
    assert r["added"] == 7
    again = client.post(f"/api/projects/{pid}/photos", files=[("files", ("red0-copy.jpg", _jpeg((200, 30, 30))))]).json()
    assert again["added"] == 0 and again["skipped_duplicates"]
    _wait(client, pid)
    prj = client.get(f"/api/projects/{pid}").json()
    by_name = {p["name"]: p for p in prj["photos"]}
    assert all(p["status"] == "ready" for p in prj["photos"])
    items = {i["id"]: i for i in prj["items"]}
    red = by_name["red1.jpg"]
    assert red["item_id"] and "стол" in (items[red["item_id"]]["title"] + items[red["item_id"]]["text"]).lower()
    blue = by_name["blue1.jpg"]
    assert blue["item_id"] and "сцен" in (items[blue["item_id"]]["title"] + items[blue["item_id"]]["text"]).lower()
    assert by_name["dark.jpg"]["enhanced"] is True
    # ручное перемещение -> подтверждено и не трогается при повторной раскладке
    target = next(i for i in prj["items"] if "уборк" in i["title"].lower())["id"]
    client.patch(f"/api/projects/{pid}/photos/{red['id']}", json={"item_id": target})
    client.post(f"/api/projects/{pid}/classify", json={})
    time.sleep(0.5)
    _wait(client, pid)
    prj = client.get(f"/api/projects/{pid}").json()
    p = next(x for x in prj["photos"] if x["id"] == red["id"])
    assert p["item_id"] == target and p["confirmed"]
    assert red["id"] in next(i for i in prj["items"] if i["id"] == target)["photo_ids"]


def test_photo_files_and_zip_upload(client):
    pid, _ = _project_with_tz(client)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Папка/a.jpg", _jpeg((10, 200, 10)))
        z.writestr("__MACOSX/._a.jpg", b"x")
        z.writestr("notes.txt", b"x")
    r = client.post(f"/api/projects/{pid}/photos", files=[("files", ("all.zip", buf.getvalue()))]).json()
    assert r["added"] == 1
    _wait(client, pid)
    ph = client.get(f"/api/projects/{pid}").json()["photos"][0]
    for kind in ("thumb", "work", "view", "before", "original"):
        assert client.get(f"/api/projects/{pid}/photos/{ph['id']}/{kind}").status_code == 200, kind
    assert client.get(f"/api/projects/{pid}/photos/{ph['id']}/../../x").status_code == 404


def test_faces_setting_reprocesses(client):
    pid, _ = _project_with_tz(client)
    client.post(f"/api/projects/{pid}/photos", files=[("files", ("a.jpg", _jpeg((120, 120, 120))))])
    _wait(client, pid)
    prj = client.patch(f"/api/projects/{pid}", json={"settings": {"blur_faces": True, "face_mode": "pixelate"}}).json()
    assert prj["settings"]["blur_faces"] is True and prj["settings"]["face_mode"] == "pixelate"
    assert prj["job"]["total"] >= 1
    _wait(client, pid)


def test_bulk_delete_and_unsorted(client):
    pid, prj = _project_with_tz(client)
    client.post(f"/api/projects/{pid}/photos", files=[("files", (f"x{i}.jpg", _jpeg((40 * i, 90, 90)))) for i in range(1, 4)])
    _wait(client, pid)
    ids = [p["id"] for p in client.get(f"/api/projects/{pid}").json()["photos"]]
    prj = client.post(f"/api/projects/{pid}/photos/bulk", json={"action": "assign", "photo_ids": ids[:2], "item_id": None}).json()
    assert prj["summary"]["unsorted"] >= 2
    prj = client.post(f"/api/projects/{pid}/photos/bulk", json={"action": "delete", "photo_ids": ids[:1]}).json()
    assert len(prj["photos"]) == 2 and all(ids[0] not in i["photo_ids"] for i in prj["items"])


def test_exports(client, tmp_path):
    pid, prj = _project_with_tz(client)
    client.post(f"/api/projects/{pid}/photos", files=[("files", ("r.jpg", _jpeg((220, 30, 30))))])
    _wait(client, pid)
    client.patch(f"/api/projects/{pid}/items/{prj['items'][0]['id']}", json={"status": "done"})
    for fmt in ("docx", "pdf", "xlsx", "zip"):
        r = client.get(f"/api/projects/{pid}/export/{fmt}")
        assert r.status_code == 200 and len(r.content) > 500, fmt
    out = client.post(f"/api/projects/{pid}/export/folders", json={"target": str(tmp_path / "out"), "open": False}).json()
    assert out["folders"]


def test_bad_ids_and_formats(client):
    assert client.get("/api/projects/../etc").status_code == 404
    assert client.get("/api/projects/p20261022-zzzz").status_code == 404
    pid = client.post("/api/projects", json={"name": "x"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/tz", files={"file": ("a.doc", b"x")}).status_code == 400
