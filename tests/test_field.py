# -*- coding: utf-8 -*-
"""Съёмка с телефона: секретная ссылка, приём кадра в пункт, отбраковка тёмных кадров, заметки офиса,
удаление только своих кадров. Данные вымышленные."""
from __future__ import annotations

import io
import time

import numpy as np
import pytest
from PIL import Image

from tests.test_no_ai import client  # noqa: F401 — та же фикстура приложения
from tests.test_server import _project_with_tz


def _sharp(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    arr = (rng.random((360, 480, 3)) * 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _black(v: int) -> bytes:
    """Почти чёрный кадр (крышка объектива, съёмка в кармане) — не спасти улучшением."""
    buf = io.BytesIO()
    Image.fromarray(np.full((360, 480, 3), v, np.uint8)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


@pytest.fixture()
def phone(client):  # noqa: F811
    from fastapi.testclient import TestClient
    from app import field
    pid, prj = _project_with_tz(client)
    links = client.post(f"/api/projects/{pid}/field").json()
    token = client.get(f"/api/projects/{pid}").json()["field"]["token"]
    with TestClient(field.field_app) as m:
        yield client, m, pid, token, links


def test_link_qr_and_state(phone):
    c, m, pid, token, links = phone
    assert links["port"] and links["auto_delete"] is True
    assert all(u.endswith("/m/" + token) for u in links["urls"])
    if links["urls"]:
        assert links["qr"].startswith("data:image/svg+xml")
    assert m.get("/m/wrong-token-123").status_code == 404
    page = m.get(f"/m/{token}")
    assert page.status_code == 200 and "Нужно снять" in page.text
    st = m.get(f"/m/{token}/state").json()
    assert st["total"] >= 2 and st["done"] == 0 and all(r["need"] for r in st["items"])


def test_accept_reject_and_delete(phone):
    c, m, pid, token, _ = phone
    iid = m.get(f"/m/{token}/state").json()["items"][0]["id"]
    # тёмный кадр при автоудалении не сохраняется, телефон просит переснять
    r = m.post(f"/m/{token}/items/{iid}/photos", files={"file": ("dark.jpg", _black(4))}).json()
    assert r["ok"] is False and r["deleted"] and "переснимите" in r["reason"]
    assert c.get(f"/api/projects/{pid}").json()["photos"] == []
    # хороший кадр — сразу в пункт, пункт выполнен
    r = m.post(f"/m/{token}/items/{iid}/photos", files={"file": ("IMG_1.jpg", _sharp(1))}).json()
    assert r["ok"] and r["id"]
    prj = c.get(f"/api/projects/{pid}").json()
    ph = prj["photos"][0]
    it = next(i for i in prj["items"] if i["id"] == iid)
    assert ph["source"] == "field" and ph["item_id"] == iid and ph["confirmed"] and it["status"] == "done"
    assert m.post(f"/m/{token}/items/{iid}/photos", files={"file": ("again.jpg", _sharp(1))}).json()["duplicate"]
    # без автоудаления слабый кадр остаётся с пометкой «переснять»
    c.patch(f"/api/projects/{pid}/field", json={"auto_delete": False})
    r = m.post(f"/m/{token}/items/{iid}/photos", files={"file": ("dark2.jpg", _black(9))}).json()
    assert r["ok"] and r["warning"]
    row = next(x for x in m.get(f"/m/{token}/state").json()["items"] if x["id"] == iid)
    assert row["count"] == 2 and row["need"] and any(p["reshoot"] for p in row["photos"])
    # с телефона удаляются только снятые с телефона кадры
    assert m.delete(f"/m/{token}/photos/{r['id']}").json()["ok"]
    office = c.post(f"/api/projects/{pid}/photos", files=[("files", ("office.jpg", _sharp(7)))]).json()
    oid = next(p["id"] for p in c.get(f"/api/projects/{pid}").json()["photos"] if p.get("source") != "field")
    assert office["added"] == 1 and m.delete(f"/m/{token}/photos/{oid}").status_code == 403
    assert m.post(f"/m/{token}/items/nope/photos", files={"file": ("x.jpg", _sharp(3))}).status_code == 404


def test_office_remark_and_new_link(phone):
    c, m, pid, token, _ = phone
    iid = m.get(f"/m/{token}/state").json()["items"][0]["id"]
    m.post(f"/m/{token}/items/{iid}/photos", files={"file": ("IMG_2.jpg", _sharp(2))})
    row = next(x for x in m.get(f"/m/{token}/state").json()["items"] if x["id"] == iid)
    assert not row["need"]
    time.sleep(1.1)                                      # время с точностью до секунды
    c.patch(f"/api/projects/{pid}/items/{iid}", json={"remark": "Переснимите со стороны входа"})
    row = next(x for x in m.get(f"/m/{token}/state").json()["items"] if x["id"] == iid)
    assert row["remark"] == "Переснимите со стороны входа" and row["need"]       # заметка новее кадра
    time.sleep(1.1)
    m.post(f"/m/{token}/items/{iid}/photos", files={"file": ("IMG_3.jpg", _sharp(3))})
    row = next(x for x in m.get(f"/m/{token}/state").json()["items"] if x["id"] == iid)
    assert not row["need"]                                                         # пересняли — готово
    # офис отметил кадр «переснять» — телефон это видит
    phid = row["photos"][0]["id"]
    c.patch(f"/api/projects/{pid}/photos/{phid}", json={"reshoot": True})
    assert next(x for x in m.get(f"/m/{token}/state").json()["items"] if x["id"] == iid)["photos"][0]["reshoot"]
    # новая ссылка: старая перестаёт работать
    c.patch(f"/api/projects/{pid}/field", json={"reset_token": True})
    assert m.get(f"/m/{token}/state").status_code == 404
    new = c.get(f"/api/projects/{pid}").json()["field"]["token"]
    assert new != token and m.get(f"/m/{new}/state").status_code == 200


def test_tunnel_url_from_cloudflared_output():
    from app.field import tunnel_url
    assert tunnel_url('failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel"') == ""
    assert tunnel_url("INF |  https://bold-sun-dog-cake.trycloudflare.com  |") == "https://bold-sun-dog-cake.trycloudflare.com"
