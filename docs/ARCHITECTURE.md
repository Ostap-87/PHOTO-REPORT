# ФотоОтчёт — архитектура (контракт между модулями)

Локальное приложение (FastAPI + одна веб-страница + окно pywebview на Mac). Python 3.11+.
Данные пользователя не покидают компьютер; модели распознавания скачиваются один раз.

```
webui/index.html  ──HTTP──►  app/server.py  ──►  app/store.py      (проекты на диске)
                                  │                app/pipeline.py   (фоновая обработка фото)
                                  ├──► app/tz/       разбор ТЗ (docx/pdf/xlsx/csv/txt) -> пункты
                                  ├──► app/vision/   фото: чтение, качество, улучшение, лица, эмбеддинги, сортировка, дубли
                                  └──► app/export/   отчёт Word / PDF / Excel, ZIP по папкам
```

## Модуль 1 — ТЗ и исполнение
Загружаем ТЗ заказчика -> слева оригинал, справа «совершенная копия»: пункты ТЗ с
статусом (Не начато / Выполнено / Предоставлено / Исполнено), комментарием и местами для фото.
Папки для фото = пункты ТЗ (создаются автоматически при загрузке ТЗ).

## Реестр подтверждений (app/registry)
ТЗ -> полный текст с подставленной нумерацией (`parse_tz(...)["full_text"]`, `tz/full_text.txt`) ->
`ai.analyze()` (Claude, задание `config/registry_prompt.md` + технические условия, ответ — JSON по схеме,
два шага: реестр и повторная сверка, `fallbacks: "default"` при отказе модели) -> `build.apply()`:
строки реестра становятся рабочими пунктами `items` (`kind: "registry"`, поля `section`, `required`, `note`,
`links`), «Требуют уточнения» — `project["clarify"]`, все пункты ТЗ — `project["tz_items"]`. Повторный анализ
сохраняет статусы и вложения у строк с тем же номером и наименованием. `build.import_xlsx()` — готовый реестр
из Excel; `export_registry_xlsx()` — выгрузка в том же виде. Ключ API — `app/secrets_store.py` (вне проектов
и репозитория, права 600). Фото раскладываются только по строкам, где требуются фото/скриншоты
(`pipeline.photo_targets`), «что приложить» служит подсказкой для распознавания.

## Модуль 2 — фото
Большой массив фото -> чтение (JPG/PNG/HEIC/TIFF/WEBP, поворот по EXIF) -> оценка качества ->
улучшение где нужно -> (галочка) замазать лица -> эмбеддинг -> раскладка по папкам-пунктам ТЗ
с уверенностью; неуверенные — в «Неразобранное» с подсказками; исправления пользователя
улучшают раскладку остальных; дубли помечаются.

## Хранение проекта: `storage/projects/<project_id>/`
```
project.json
tz/<исходный файл ТЗ>
tz/source.html            оригинал ТЗ для левой панели (docx/xlsx/txt), фрагменты с data-src
tz/pages/page-001.png     страницы PDF-оригинала (для pdf)
photos/original/<photo_id><ext>   исходник — НЕ изменяется
photos/work/<photo_id>.jpg        рабочая копия: улучшение и/или скрытые лица (если не нужно — копия исходника в JPG)
photos/thumb/<photo_id>.jpg       превью 480 px по длинной стороне
photos/files/<file_id><ext>       вложения-файлы пунктов (документы, видео), project["files"]
tz/full_text.txt                  полный текст ТЗ с подставленной нумерацией (для анализа ИИ)
emb.npy, emb_ids.json             эмбеддинги фото (N×D float32, L2-нормированы) и их photo_id
```

## project.json
```json
{
  "id": "p20261022-ab12", "name": "Банкет 25.10", "customer": "ООО «Пример»", "created": "...", "updated": "...",
  "settings": {"enhance": "auto", "blur_faces": false, "face_mode": "blur"},
  "tz": {"file": "tz/ТЗ.docx", "kind": "docx", "title": "...", "html": "tz/source.html", "pages": 0},
  "items": [
    {"id": "it1", "num": "1.2", "level": 2, "title": "Расстановка столов", "text": "полный текст пункта",
     "qty": 12, "unit": "шт", "source": {"kind": "para", "index": 14, "page": null},
     "status": "todo", "comment": "", "done_at": "", "hint": "", "photo_ids": []}
  ],
  "photos": [
    {"id": "ph_3f2a9c", "name": "IMG_0001.HEIC", "ext": ".heic", "sha256": "...", "w": 4032, "h": 3024,
     "exif": {"datetime": "2026-10-21 14:03:11", "gps": [55.75, 37.62], "camera": "Apple iPhone 15"},
     "quality": {"brightness": 0.31, "contrast": 0.12, "blur": 85.0, "flags": ["dark"], "needs_enhance": true, "reshoot": false},
     "enhanced": true, "applied": ["exposure", "clahe", "sharpen"], "faces": 2, "faces_blurred": true,
     "item_id": "it3", "confidence": 0.81, "suggestions": [["it3", 0.81], ["it5", 0.10]],
     "confirmed": false, "duplicate_of": null, "status": "ready", "error": ""}
  ]
}
```
- `item.status`: `todo` | `done` (Выполнено) | `provided` (Предоставлено) | `executed` (Исполнено) | `na` (Не требуется).
- `item.level`: 1 — раздел/пункт верхнего уровня, 2+ — подпункты. Фото можно привязать к любому уровню.
- `item.hint`: подсказка «что на фото» (заполняет пользователь), усиливает автосортировку.
- `photo.item_id = null` — «Неразобранное». `confirmed = true` — положено/подтверждено человеком
  (используется как пример для раскладки остальных). Порядок фото в пункте — `item.photo_ids`.

## Интерфейсы модулей

### app/tz (разбор ТЗ)
```python
parse_tz(data: bytes, filename: str) -> dict
# {"title": str, "kind": "docx|pdf|xlsx|csv|txt",
#  "items": [{"num","level","title","text","qty","unit","source":{"kind","index","page"}}],   # без id/status — их ставит store
#  "html": str | None,          # оригинал для левой панели: элементы с data-src="p<index>" / "t<table>-r<row>"
#  "pages": [bytes PNG] | []}   # страницы PDF
```
### app/export (отчёты)
```python
export_docx(project: dict, project_dir: str, out_path: str, options: dict) -> str
export_pdf(project: dict, project_dir: str, out_path: str, options: dict) -> str
export_xlsx(project: dict, project_dir: str, out_path: str) -> str
export_zip(project: dict, project_dir: str, out_path: str, which: "work"|"original") -> str   # папки «<№> <пункт>/…»
# options: {"photos_per_row": 2, "include_unsorted": False, "only_status": None}
```
### app/vision (фото)
```python
io.load_image(path) -> (rgb: np.ndarray HxWx3 uint8, exif_bytes: bytes|None, meta: dict)   # поворот по EXIF, HEIC
io.save_jpeg(rgb, path, exif_bytes=None, quality=92) -> None
io.thumbnail(rgb, max_side=480) -> np.ndarray
quality.analyze(rgb) -> dict                      # brightness, contrast, blur, noise, flags, needs_enhance, reshoot
enhance.enhance(rgb, quality: dict, mode="auto") -> (rgb, list[str])   # mode: auto|all|off
faces.detect(rgb) -> list[tuple[int,int,int,int,float]]               # x, y, w, h, score
faces.blur(rgb, boxes, mode="blur"|"pixelate") -> rgb
embed.Embedder(models_dir).status() / .ensure(progress_cb) / .encode_images(list[rgb]) / .encode_texts(list[str])
classify.build_prompts(item: dict) -> list[str]
classify.classify(photo_embs, item_text_embs: dict[str, ndarray], examples: dict[str, ndarray], params: dict) -> list[dict]
#   -> [{"item_id": str|None, "confidence": float, "suggestions": [[item_id, p], ...]}]
dedup.dhash(rgb) -> int; dedup.groups(hashes: list[int], max_dist=6) -> list[list[int]]
```
