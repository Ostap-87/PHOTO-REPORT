# -*- coding: utf-8 -*-
"""Реестр подтверждений: анализ ИИ (с подменённым клиентом, без сети и ключа), перенос в проект,
импорт из Excel, ключ API, вложения-файлы. Все данные вымышленные."""
from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace

import pytest

ROWS = [
    {"num": "6.34", "section": "Фото- и видеосъёмка", "name": "Фотосъёмка мероприятий",
     "description": "Не менее 20 фото с каждого мероприятия", "required": "Не менее 20 обработанных фотографий",
     "comment": ""},
    {"num": "7.1.3", "section": "Марафон", "name": "Скриншоты Марафона", "description": "Скриншоты процесса",
     "required": "Скриншоты в количестве 15 штук", "comment": ""},
    {"num": "7.1.1", "section": "Отчётность", "name": "Список участников", "description": "Список по форме",
     "required": "Список в бумажном виде и Excel", "comment": "Форма списка не приложена"},
]
RESULT = {"rows": ROWS, "clarify": [{"num": "6.7", "section": "Оформление", "name": "Фотозона",
                                     "description": "Передача архива", "reason": "Не указан получатель"}],
          "summary": {"analyzed": "3 раздела, 12 пунктов", "sections": ["Фото", "Отчётность"], "issues": "нет"}}


class FakeStream:
    def __init__(self, payload, calls, kwargs):
        self.payload, self.calls, self.kwargs = payload, calls, kwargs

    def __enter__(self):
        self.calls.append(self.kwargs)
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=json.dumps(self.payload, ensure_ascii=False))],
            stop_reason="end_turn", model="claude-opus-5-5",
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


class FakeClient:
    def __init__(self, payloads):
        self.payloads, self.calls = list(payloads), []
        outer = self

        class _M:
            def stream(self, **kw):
                return FakeStream(outer.payloads.pop(0), outer.calls, kw)

        self.beta = SimpleNamespace(messages=_M())


def test_analyze_two_passes_with_task_prompt():
    from app.registry import ai
    draft = dict(RESULT, rows=ROWS[:2])
    client = FakeClient([draft, RESULT])
    out = ai.analyze("6.34. Фотосъёмка…\n7.1.3. Скриншоты…", "ТЗ форума", client=client)
    assert len(out["rows"]) == 3 and out["clarify"][0]["reason"] == "Не указан получатель"
    first, second = client.calls
    assert first["model"] == "claude-opus-5-5" and "Детальный анализ технического задания" in first["system"]
    assert first["output_config"]["format"]["type"] == "json_schema" and first["fallbacks"] == "default"
    assert "Обязательная повторная проверка" in second["messages"][-1]["content"]
    assert second["messages"][1]["role"] == "assistant"            # ответ первого шага возвращён без изменений
    assert out["usage"]["input_tokens"] == 2000


def test_analyze_empty_text_is_clear_error():
    from app.registry import ai
    with pytest.raises(ai.AIError):
        ai.analyze("   ", client=FakeClient([]))


def test_apply_rebuild_keeps_state_and_unsorts_old_items():
    from app import store
    from app.registry import build
    prj = {"id": "p20261022-0001", "items": [], "photos": [], "next_item": 1}
    tz = [store.new_item(prj, num="6.34", title="Фото", source={"ref": "p10"}),
          store.new_item(prj, num="7.1.3", title="Скриншоты", source={"ref": "p20"})]
    prj["items"], prj["tz_items"] = list(tz), [dict(t) for t in tz]
    prj["photos"] = [{"id": "ph_1", "item_id": tz[0]["id"], "confirmed": True}]
    tz[0]["photo_ids"] = ["ph_1"]
    build.apply(prj, RESULT)
    assert [it["kind"] for it in prj["items"]] == ["registry"] * 3
    assert prj["items"][0]["source"] == {"ref": "p10"} and prj["items"][0]["required"].startswith("Не менее 20")
    assert prj["photos"][0]["item_id"] is None                      # фото раскладываются заново по реестру
    store.assign(prj, "ph_1", prj["items"][0]["id"])
    prj["items"][0]["status"] = "provided"
    build.apply(prj, RESULT)                                        # повторный анализ — состояние сохраняется
    assert prj["items"][0]["status"] == "provided" and prj["photos"][0]["item_id"] == prj["items"][0]["id"]
    assert prj["registry"]["rows"] == 3 and prj["clarify"][0]["num"] == "6.7"


def test_import_registry_xlsx():
    from openpyxl import Workbook
    from app.registry import build
    wb = Workbook()
    ws = wb.active
    ws.title = "Реестр подтверждений"
    ws.append(["Пункт ТЗ", "Раздел", "Наименование", "Описание", "Что конкретно должно быть приложено, в каком виде",
               "Комментарий"])
    for r in ROWS:
        ws.append([r["num"], r["section"], r["name"], r["description"], r["required"], r["comment"]])
    ws2 = wb.create_sheet("Требуют уточнения")
    ws2.append(["Пункт ТЗ", "Раздел", "Наименование", "Описание", "Причина неопределённости"])
    ws2.append(["6.7", "Оформление", "Фотозона", "Архив", "Не указан получатель"])
    buf = io.BytesIO()
    wb.save(buf)
    res = build.import_xlsx(buf.getvalue())
    assert len(res["rows"]) == 3 and res["rows"][1]["required"] == "Скриншоты в количестве 15 штук"
    assert res["clarify"][0]["reason"] == "Не указан получатель"
    with pytest.raises(ValueError):
        build.import_xlsx(b"not excel")


def test_photo_targets_only_visual_rows():
    from app import pipeline
    prj = {"items": [dict(r, id=f"it{i}", kind="registry", title=r["name"], text=r["description"], status="todo")
                     for i, r in enumerate(ROWS)]}
    assert [it["id"] for it in pipeline.photo_targets(prj)] == ["it0", "it1"]   # список участников — без фото


# ------------------------------------------------------------------ сервер
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_REPORT_HOME", str(tmp_path / "projects"))
    monkeypatch.setenv("PHOTO_REPORT_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("PHOTO_REPORT_NO_DOWNLOAD", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    import app.server as server
    with TestClient(server.app) as c:
        yield c


TZ = "Техническое задание\n\n6.34 Фотосъёмка мероприятий\n7.1.3 Скриншоты Марафона\n7.1.1 Список участников\n"


def test_ai_key_never_echoed(client):
    st = client.get("/api/ai/status").json()
    assert st["configured"] is False
    assert client.put("/api/ai/key", json={"api_key": "hello"}).status_code == 400
    key = "sk-ant-api03-" + "x" * 40 + "abcd"
    st = client.put("/api/ai/key", json={"api_key": key}).json()
    assert st["configured"] and st["source"] == "settings" and key not in json.dumps(st) and st["masked"].endswith("abcd")
    assert client.delete("/api/ai/key").json()["configured"] is False


def test_registry_build_endpoint(client, monkeypatch):
    from app.registry import ai
    pid = client.post("/api/projects", json={"name": "Форум"}).json()["id"]
    client.post(f"/api/projects/{pid}/tz", files={"file": ("tz.txt", TZ.encode())})
    assert client.post(f"/api/projects/{pid}/registry/build", json={"mode": "ai"}).status_code == 409   # нет ключа
    client.put("/api/ai/key", json={"api_key": "sk-ant-api03-" + "y" * 44})
    seen = {}

    def fake(text, title="", **kw):
        seen["text"] = text
        return dict(RESULT, model="claude-opus-5-5", usage={})
    monkeypatch.setattr(ai, "analyze", fake)
    assert client.post(f"/api/projects/{pid}/registry/build", json={"mode": "ai"}).json()["started"]
    for _ in range(100):
        prj = client.get(f"/api/projects/{pid}").json()
        if prj.get("registry"):
            break
        time.sleep(0.05)
    assert "6.34" in seen["text"] and "7.1.3" in seen["text"]
    assert len(prj["items"]) == 3 and prj["items"][1]["required"] == "Скриншоты в количестве 15 штук"
    assert len(prj["tz_items"]) >= 3 and prj["clarify"][0]["num"] == "6.7"
    # ручная строка из пункта ТЗ и возврат к пунктам ТЗ
    r = client.post(f"/api/projects/{pid}/registry/rows", json={"tz_id": prj["tz_items"][0]["id"]}).json()
    assert r["item"]["kind"] == "registry"
    prj = client.delete(f"/api/projects/{pid}/registry").json()
    assert not prj.get("registry") and all(it["kind"] == "tz" for it in prj["items"])


def test_file_attachments(client):
    pid = client.post("/api/projects", json={"name": "Файлы"}).json()["id"]
    client.post(f"/api/projects/{pid}/tz", files={"file": ("tz.txt", TZ.encode())})
    iid = client.get(f"/api/projects/{pid}").json()["items"][0]["id"]
    r = client.post(f"/api/projects/{pid}/photos", files=[("files", ("отчёт.pdf", b"%PDF-1.4 test"))]).json()
    assert r["files_added"] == 0 and r["rejected"] == ["отчёт.pdf"]                    # без пункта — нельзя
    r = client.post(f"/api/projects/{pid}/photos", files=[("files", ("отчёт.pdf", b"%PDF-1.4 test"))],
                    data={"item_id": iid}).json()
    assert r["files_added"] == 1
    f = client.get(f"/api/projects/{pid}").json()["files"][0]
    assert f["item_id"] == iid and client.get(f"/api/projects/{pid}/files/{f['id']}").content.startswith(b"%PDF")
    out = client.patch(f"/api/projects/{pid}/items/{iid}", json={"links": ["https://example.org/post", " "]}).json()
    assert out["links"] == ["https://example.org/post"]
    assert client.delete(f"/api/projects/{pid}/files/{f['id']}").status_code == 200
