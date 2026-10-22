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


def test_perfect_text_and_folders(client):
    from app.tz.perfect import perfect
    assert perfect("Исполнитель обеспечивает площадку и согласовывает макет.") == \
        "Исполнитель обеспечил площадку и согласовал макет."
    assert perfect("Площадка должна соответствовать нормам.") == "Площадка соответствовала нормам."
    assert perfect("Фотозона должна быть брендирована.") == "Фотозона была брендирована."
    assert perfect("Обеспечить наличие стенда.") == "Обеспечили наличие стенда."
    assert perfect("Регистрация ведётся на сайте.") == "Регистрация ведётся на сайте."   # страдательный — как есть
    pid = client.post("/api/projects", json={"name": "Форум"}).json()["id"]
    client.post(f"/api/projects/{pid}/tz", files={"file": ("tz_forum_table.docx", _sample("tz_forum_table.docx"))})
    prj = client.get(f"/api/projects/{pid}").json()
    it = next(x for x in prj["items"] if x["title"].startswith("Исполнитель обеспечивает площадку"))
    assert it["pf_title"].startswith("Исполнитель обеспечил площадку")
    # ручная правка текста отчёта сохраняется, сброс — пересчитывает
    out = client.patch(f"/api/projects/{pid}/items/{it['id']}", json={"pf_title": "Площадка обеспечена"}).json()
    assert out["pf_title"] == "Площадка обеспечена" and out["pf_manual"]
    out = client.patch(f"/api/projects/{pid}/items/{it['id']}", json={"pf_reset": True}).json()
    assert out["pf_title"].startswith("Исполнитель обеспечил")
    # папки: своя папка и переименование; своя папка переживает составление реестра
    r = client.post(f"/api/projects/{pid}/items", json={"title": "Дополнительные фото", "kind": "custom"}).json()
    assert r["item"]["kind"] == "custom"
    client.patch(f"/api/projects/{pid}/items/{it['id']}", json={"folder": "Площадка"})
    from app.export import folder_names
    names = folder_names(client.get(f"/api/projects/{pid}").json())
    assert any(n.endswith("Площадка") for n in names.values())
    prj = client.post(f"/api/projects/{pid}/registry/build", json={"mode": "rules"}).json()["project"]
    assert any(x["id"] == r["item"]["id"] for x in prj["items"])
    # «ТЗ с фото»: текст ТЗ в совершенном виде (по умолчанию) и как есть (perfect=false)
    client.patch(f"/api/projects/{pid}/items/{prj['items'][0]['id']}", json={"status": "done"})
    full = "".join(t.text or "" for t in Document(io.BytesIO(
        client.get(f"/api/projects/{pid}/export/tzdocx").content)).element.body.iter(qn("w:t")))
    assert "Исполнитель обеспечил площадку" in full and "обеспечивает площадку" not in full
    raw = "".join(t.text or "" for t in Document(io.BytesIO(
        client.get(f"/api/projects/{pid}/export/tzdocx?perfect=false").content)).element.body.iter(qn("w:t")))
    assert "Исполнитель обеспечивает площадку" in raw


def test_import_from_folder(client, tmp_path):
    src = tmp_path / "flash" / "День 1"
    src.mkdir(parents=True)
    (src / "a.jpg").write_bytes(_jpeg((200, 30, 30)))
    (src / "b.jpg").write_bytes(_jpeg((30, 30, 200)))
    (src / "notes.txt").write_text("не фото")
    pid = client.post("/api/projects", json={"name": "Флешка"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/photos/import", json={"path": str(tmp_path / "flash")}).json()
    assert r["started"]
    prj = _wait_photos(client, pid, 2)
    assert len(prj["photos"]) == 2
    assert client.post(f"/api/projects/{pid}/photos/import", json={"path": str(tmp_path / "нет")}).status_code == 400


def test_gdrive_folder_page_parsing():
    from app import sources
    page = ('<div class="flip-entry" id="entry-1AbCdEfGhIj"><a href="https://drive.google.com/file/d/1AbCdEfGhIj/view">'
            '<div class="flip-entry-title">IMG_0001.jpg</div></a></div>'
            '<div class="flip-entry" id="entry-9ZyXwVuTsRq"><a href="https://drive.google.com/drive/folders/9ZyXwVuTsRq">'
            '<div class="flip-entry-title">День 2</div></a></div>')
    files, folders = sources.parse_gdrive_folder(page)
    assert files == [("1AbCdEfGhIj", "IMG_0001.jpg")] and folders == ["9ZyXwVuTsRq"]
    assert sources.kind_of("https://disk.yandex.ru/d/abc") == "yandex"
    assert sources.kind_of("https://drive.google.com/drive/folders/xyz") == "gdrive"
    with pytest.raises(sources.SourceError):
        sources.download("ftp://example.org/x", str("/tmp"))


def test_pptx_export_and_preview(client):
    from pptx import Presentation
    pid = client.post("/api/projects", json={"name": "Слайды"}).json()["id"]
    client.post(f"/api/projects/{pid}/tz", files={"file": ("tz_forum_table.docx", _sample("tz_forum_table.docx"))})
    prj = client.get(f"/api/projects/{pid}").json()
    it = next(x for x in prj["items"] if x["title"].startswith("Исполнитель обеспечивает площадку"))
    client.post(f"/api/projects/{pid}/photos", files=[("files", (f"{i}.jpg", _jpeg((40 + i * 20, 90, 160))))
                                                       for i in range(8)])
    prj = _wait_photos(client, pid, 8)
    for ph in prj["photos"]:
        client.patch(f"/api/projects/{pid}/photos/{ph['id']}", json={"item_id": it["id"]})
    client.patch(f"/api/projects/{pid}/items/{it['id']}", json={"status": "done"})
    r = client.get(f"/api/projects/{pid}/export/pptx")
    assert r.status_code == 200
    prs = Presentation(io.BytesIO(r.content))
    slides = list(prs.slides)
    assert len(slides) == 3                                  # титул + 6 фото + «продолжение» с 2 фото
    texts = [" ".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame) for s in slides]
    assert "Исполнитель обеспечил площадку" in texts[1] and "(продолжение)" in texts[2]
    assert sum(1 for sh in slides[1].shapes if sh.shape_type == 13) == 6
    pv = client.get(f"/api/projects/{pid}/preview/pptx").json()
    assert len(pv["pages"]) == 3 and client.get(pv["pages"][1].split("?")[0]).status_code == 200
    pv = client.get(f"/api/projects/{pid}/preview/pdf").json()
    assert pv["pages"] and client.get(pv["pages"][0].split("?")[0]).headers["content-type"] == "image/png"
    assert client.get(f"/api/projects/{pid}/preview-img/pdf/..%2Fproject.json").status_code == 404
