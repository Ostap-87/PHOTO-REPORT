"""Разбор ТЗ заказчика в пункты.

    parse_tz(data: bytes, filename: str) -> dict

Возвращает:
    {
      "title": str,                        # «Техническое задание на …» или имя файла
      "kind": "docx|pdf|xlsx|csv|txt",     # .xls → "xlsx", .md → "txt"
      "items": [{"num", "level", "title", "text", "qty", "unit",
                 "source": {"kind", "index", "page", "ref", …}}],
      "html": str | None,                  # оригинал для левой панели (docx/xlsx/csv/txt), для PDF — None
      "pages": [bytes PNG] | [],           # страницы PDF (~110 dpi)
      "warnings": [str],                   # пояснения для пользователя (скан, нет пунктов…)
      "full_text": str,                    # весь документ простым текстом по порядку (для ИИ-анализа)
    }

full_text: каждый абзац — с новой строки и со своим настоящим номером (автонумерация Word
вставлена в текст: «6.15. …»), маркированные списки Word — «- …». Строка таблицы — ячейки
через « | », абзацы ячейки — с новой строки. PDF — текст страниц с разделителями «--- стр. N ---»,
Excel/CSV — строка на строку таблицы, ячейки через « | » (несколько листов — «--- лист «Имя» ---»),
txt/md — исходный текст. Колонтитулы Word не включаются.

Строки таблицы-раздела «№ | Наименование раздела | Требования» в Word раскрываются: раздел — пункт
уровня 1, каждый нумерованный абзац ячейки — свой пункт (source.kind = "cell",
ref = "t<таблица>-r<строка>-p<абзац>", абзацы нумеруются сквозь всю строку таблицы с 0).

source.ref — точное значение data-src фрагмента в html («p14» — абзац, «t0-r3» — строка
таблицы); подсветка: document.querySelector('[data-src="' + ref + '"]'). У пунктов из PDF
ref = None, page — номер страницы с 1, bbox — [x0, y0, x1, y1] в долях страницы.

Весь текст в html экранирован; тегов script/style и обработчиков событий нет, только
теги разметки (div, p, h1–h6, b, i, u, br, span, table/tbody/tr/td) с классами tz-*,
data-src и простым inline-стилем отступа/выравнивания.

Неподдерживаемый или повреждённый файл — ValueError с понятным русским сообщением.
"""

from __future__ import annotations

import os

SUPPORTED_EXTENSIONS = (".docx", ".pdf", ".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".txt", ".md", ".markdown")


def parse_tz(data: bytes, filename: str) -> dict:
    """Разбирает файл ТЗ (docx, pdf, xlsx/xls, csv, txt/md) в пункты и HTML-оригинал."""
    if not data:
        raise ValueError("Файл пустой")
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".doc":
        raise ValueError("Старый формат Word (.doc) не поддерживается: откройте файл в Word и сохраните как .docx")
    if ext in ("", ".bin") or ext not in SUPPORTED_EXTENSIONS:
        ext = _sniff(data, ext)

    if ext == ".docx":
        from .docx_parser import parse_docx
        return parse_docx(data, filename)
    if ext == ".pdf":
        from .pdf_parser import parse_pdf
        return parse_pdf(data, filename)
    if ext in (".xlsx", ".xlsm"):
        from .tables_parser import parse_xlsx
        return parse_xlsx(data, filename)
    if ext == ".xls":
        from .tables_parser import parse_xls
        return parse_xls(data, filename)
    if ext in (".csv", ".tsv"):
        from .tables_parser import parse_csv
        return parse_csv(data, filename)
    from .text_parser import parse_text
    return parse_text(data, filename)


def _sniff(data: bytes, ext: str) -> str:
    """Определяет формат по содержимому, если расширение незнакомое."""
    head = data[:8]
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"PK"):
        import io
        import zipfile
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except zipfile.BadZipFile:
            names = []
        if any(n.startswith("word/") for n in names):
            return ".docx"
        if any(n.startswith("xl/") for n in names):
            return ".xlsx"
        raise ValueError("Неизвестный формат архива: нужен .docx, .xlsx или .pdf")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        raise ValueError("Старый формат Office (.doc/.xls): сохраните файл как .docx или .xlsx")
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError(f"Формат файла {ext or 'без расширения'} не поддерживается: "
                         "загрузите .docx, .pdf, .xlsx, .csv или .txt")
    return ".txt"


__all__ = ["parse_tz", "SUPPORTED_EXTENSIONS"]
