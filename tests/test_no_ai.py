# -*- coding: utf-8 -*-
"""Работа без ИИ: реестр подтверждений по правилам и выгрузка «ТЗ с фото» (фото в исходном Word под
пунктами). Данные вымышленные: samples/tz_forum_table.docx и tz_banket.docx."""
from __future__ import annotations

import io
import os
import time

import pytest
from docx import Document
from docx.oxml.ns import qn

from tests.test_server import FakeEmbedder, _jpeg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sample(name: str) -> bytes:
    with open(os.path.join(ROOT, "samples", name), "rb") as fh:
        return fh.read()


def test_rules_registry_counts_forms_and_report_section():
    from app.registry import rules
    items = [
        {"num": "5", "title": "Фото- и видеосъёмка", "text": "Фото- и видеосъёмка", "level": 1},
        {"num": "5.1", "level": 2, "title": "Исполнитель предоставляет Заказчику фотографии",
         "text": "Исполнитель предоставляет Заказчику не менее 200 (двухсот) фотографий в электронном виде "
                 "на электронном носителе. Фотозона оформляется по согласованию с Заказчиком."},
        {"num": "5.2", "level": 2, "title": "Площадка", "text": "Площадка должна вмещать 300 участников."},
        {"num": "5.3", "level": 2, "title": "Оснащение зала",
         "text": "Оснащение зала: проектор – 1 (одна) шт., микрофоны – 4 шт."},
        {"num": "6", "title": "Требования к отчёту", "text": "Требования к отчёту", "level": 1},
        {"num": "6.1", "level": 2, "title": "Скриншоты в количестве 15 штук",
         "text": "Скриншоты в количестве 15 штук, подтверждающие публикации (пункт 5.1. Технического задания), "
                 "в электронном виде."},
    ]
    res = rules.analyze(items)
    rows = {r["num"]: r for r in res["rows"]}
    assert set(rows) == {"5.1", "5.1, 6.1"}                              # площадка без подтверждения — нет
    assert rows["5.1"]["required"].startswith("Не менее 200") and "электронном носителе" in rows["5.1"]["required"]
    assert rows["5.1, 6.1"]["section"] == "Требования к отчёту"
    assert res["clarify"][0]["num"] == "5.1" and "согласованию" in res["clarify"][0]["reason"]
    with_eq = {r["num"] for r in rules.analyze(items, with_equipment=True)["rows"]}
    assert "5.3" in with_eq                                             # оснащение с количествами — «фото»


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_REPORT_HOME", str(tmp_path / "projects"))
    monkeypatch.setenv("PHOTO_REPORT_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("PHOTO_REPORT_NO_DOWNLOAD", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    from app import pipeline
    import app.server as server
    pipeline.MODELS._emb = FakeEmbedder()
    with TestClient(server.app) as c:
        yield c


def _wait_photos(client, pid, n):
    for _ in range(100):
        prj = client.get(f"/api/projects/{pid}").json()
        if len(prj["photos"]) >= n and not client.get(f"/api/projects/{pid}/status").json()["job"].get("running"):
            return prj
        time.sleep(0.05)
    return client.get(f"/api/projects/{pid}").json()


def test_rules_endpoint_without_key_and_tz_with_photos(client):
    pid = client.post("/api/projects", json={"name": "Форум"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/tz", files={"file": ("tz_forum_table.docx",
                                                                  _sample("tz_forum_table.docx"))}).status_code == 200
    r = client.post(f"/api/projects/{pid}/registry/build", json={"mode": "rules"}).json()   # ключа нет — работает
    assert r["done"] and r["rows"] >= 4 and r["project"]["registry"]["mode"] == "rules"
    prj = r["project"]
    shots = next(it for it in prj["items"] if it["num"].endswith("6.1.2"))
    assert "15" in shots["required"] and shots["source"]["ref"].startswith("t0-r6")
    client.post(f"/api/projects/{pid}/photos", files=[("files", ("a.jpg", _jpeg((200, 40, 40)))),
                                                       ("files", ("b.jpg", _jpeg((40, 40, 200))))])
    prj = _wait_photos(client, pid, 2)
    for ph in prj["photos"]:
        assert client.patch(f"/api/projects/{pid}/photos/{ph['id']}", json={"item_id": shots["id"]}).status_code == 200
    client.patch(f"/api/projects/{pid}/items/{shots['id']}", json={"status": "done", "comment": "Скриншоты сданы"})

    out = client.get(f"/api/projects/{pid}/export/tzdocx")
    assert out.status_code == 200 and "tz-with-photos" not in out.headers["content-disposition"]
    doc = Document(io.BytesIO(out.content))
    body = doc.element.body
    # отметка и фото — в той же ячейке таблицы, что пункт 6.1.2, сразу после его абзаца
    cell = None
    for tc in body.iter(qn("w:tc")):
        txt = "".join(t.text or "" for t in tc.iter(qn("w:t")))
        if "Скриншоты в количестве 15" in txt and "Исполнение:" in txt:
            cell = tc
    assert cell is not None
    paras = ["".join(t.text or "" for t in p.iter(qn("w:t"))) for p in cell.findall(qn("w:p"))]
    i_item = next(i for i, t in enumerate(paras) if t.startswith("Скриншоты в количестве 15"))
    i_mark = next(i for i, t in enumerate(paras) if "Исполнение:" in t)
    assert i_mark == i_item + 1 and "Выполнено" in paras[i_mark] and "Скриншоты сданы" in paras[i_mark]
    assert len(list(cell.iter(qn("w:drawing")))) == 2                     # оба фото в этой ячейке
    assert cell[-1].tag == qn("w:p")                                     # ячейка заканчивается абзацем
    # остальной текст ТЗ на месте
    full = "".join(t.text or "" for t in body.iter(qn("w:t")))
    assert "Видеозапись главной сессии Форума" in full and "Акт оказанных услуг" in full


def test_tz_with_photos_body_paragraphs_and_non_word(client):
    pid = client.post("/api/projects", json={"name": "Банкет"}).json()["id"]
    client.post(f"/api/projects/{pid}/tz", files={"file": ("tz_banket.docx", _sample("tz_banket.docx"))})
    prj = client.get(f"/api/projects/{pid}").json()
    target = next(it for it in prj["items"] if it["source"] and it["source"]["ref"].startswith("p")
                  and it.get("level", 1) >= 2)
    nxt = prj["items"][prj["items"].index(target) + 1]
    client.post(f"/api/projects/{pid}/photos", files=[("files", ("c.jpg", _jpeg((40, 200, 40))))])
    prj = _wait_photos(client, pid, 1)
    client.patch(f"/api/projects/{pid}/photos/{prj['photos'][0]['id']}", json={"item_id": target["id"]})
    doc = Document(io.BytesIO(client.get(f"/api/projects/{pid}/export/tzdocx").content))
    blocks = list(doc.element.body.iterchildren())
    texts = ["".join(t.text or "" for t in b.iter(qn("w:t"))) for b in blocks]
    i_mark = next(i for i, t in enumerate(texts) if t.startswith("Исполнение:"))
    assert blocks[i_mark + 1].tag == qn("w:tbl") and list(blocks[i_mark + 1].iter(qn("w:drawing")))
    i_next = next(i for i, t in enumerate(texts) if nxt["title"][:25] in t)
    assert i_mark < i_next                                              # до следующего пункта
    assert target["title"][:25] in " ".join(texts[:i_mark])

    pid2 = client.post("/api/projects", json={"name": "PDF"}).json()["id"]
    client.post(f"/api/projects/{pid2}/tz", files={"file": ("tz_uborka.pdf", _sample("tz_uborka.pdf"))})
    r = client.get(f"/api/projects/{pid2}/export/tzdocx")
    assert r.status_code == 400 and "Word" in r.json()["detail"]
