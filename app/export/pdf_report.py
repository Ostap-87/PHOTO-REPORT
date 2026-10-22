"""Отчёт PDF (reportlab, A4 книжная): титул, таблица исполнения, пункты с фото, колонтитул «стр. N из M».

Кириллица: шрифт ищется в системе (DejaVu Sans на Linux, Arial на macOS и Windows,
Arial Unicode на macOS как запасной). Путь можно задать переменной PHOTOREPORT_FONT
(и PHOTOREPORT_FONT_BOLD) или положить TTF в app/export/fonts/.
"""

from __future__ import annotations

import datetime as _dt
import io
import os

from .common import (STATUS_COLORS, STATUS_LABELS, STATUS_TINTS, ImageCache, PhotoIndex, fmt_date, item_label,
                     norm_options, only_status_text, period_text, photo_caption, qty_text, same_text, section_info,
                     select_items, status_key, summary)

_HERE = os.path.dirname(os.path.abspath(__file__))
FONT_CANDIDATES: list[tuple[str, str]] = [
    (os.path.join(_HERE, "fonts", "DejaVuSans.ttf"), os.path.join(_HERE, "fonts", "DejaVuSans-Bold.ttf")),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
]
_FONTS: tuple[str, str] | None = None


def find_font() -> tuple[str, str] | None:
    """Пути к обычному и жирному TTF с кириллицей (жирный может совпадать с обычным)."""
    env = os.environ.get("PHOTOREPORT_FONT")
    cands = list(FONT_CANDIDATES)
    if env:
        cands.insert(0, (env, os.environ.get("PHOTOREPORT_FONT_BOLD") or env))
    windir = os.environ.get("WINDIR")
    if windir:
        cands.append((os.path.join(windir, "Fonts", "arial.ttf"), os.path.join(windir, "Fonts", "arialbd.ttf")))
    for reg, bold in cands:
        if os.path.isfile(reg):
            return reg, bold if os.path.isfile(bold) else reg
    return None


def _register_fonts() -> tuple[str, str]:
    """Регистрирует шрифты в reportlab; без кириллического шрифта — Helvetica (буквы будут квадратами)."""
    global _FONTS
    if _FONTS:
        return _FONTS
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    found = find_font()
    if not found:
        _FONTS = ("Helvetica", "Helvetica-Bold")
        return _FONTS
    reg, bold = found
    pdfmetrics.registerFont(TTFont("PRSans", reg))
    pdfmetrics.registerFont(TTFont("PRSans-Bold", bold))
    addMapping("PRSans", 0, 0, "PRSans")
    addMapping("PRSans", 1, 0, "PRSans-Bold")
    addMapping("PRSans", 0, 1, "PRSans")
    addMapping("PRSans", 1, 1, "PRSans-Bold")
    _FONTS = ("PRSans", "PRSans-Bold")
    return _FONTS


def _x(s) -> str:
    """Экранирование для Paragraph reportlab (мини-разметка)."""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\n", "<br/>"))


def export_pdf(project: dict, project_dir: str, out_path: str, options: dict | None = None) -> str:
    """Отчёт PDF A4: титул со сводкой, таблица исполнения, пункты с фото сеткой; возвращает out_path."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import (CondPageBreak, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    opts = norm_options(options)
    items = select_items(project, opts)
    idx = PhotoIndex(project, opts["skip_duplicates"])
    cache = ImageCache(project_dir, opts["max_px"], opts["jpeg_quality"])
    s = summary(project)
    sections = section_info(project, idx)
    reg, bold = _register_fonts()
    proj_name = (project.get("name") or "Фотоотчёт").strip()

    def c(hex6: str):
        return colors.HexColor("#" + hex6)

    base = ParagraphStyle("base", fontName=reg, fontSize=9.5, leading=12.5, textColor=c("202124"))
    small = ParagraphStyle("small", parent=base, fontSize=8, leading=10, textColor=c("5F6368"))
    caption = ParagraphStyle("caption", parent=small, fontSize=7, leading=8.6, alignment=TA_CENTER)
    muted = ParagraphStyle("muted", parent=base, fontSize=8.5, textColor=c("3C4043"))
    note = ParagraphStyle("note", parent=small, textColor=c("9AA0A6"))
    cell = ParagraphStyle("cell", parent=base, fontSize=8.3, leading=10.2)
    cell_b = ParagraphStyle("cellb", parent=cell, fontName=bold)
    cell_head = ParagraphStyle("cellh", parent=cell, fontName=bold, textColor=colors.white)
    h1 = ParagraphStyle("h1", parent=base, fontName=bold, fontSize=15, leading=19, spaceBefore=4, spaceAfter=8)
    h2 = ParagraphStyle("h2", parent=base, fontName=bold, fontSize=11.5, leading=14.5, spaceBefore=10, spaceAfter=3)
    h3 = ParagraphStyle("h3", parent=h2, fontSize=10.3, leading=13, spaceBefore=8, textColor=c("3C4043"))
    kicker = ParagraphStyle("kicker", parent=base, fontName=bold, fontSize=10.5, textColor=c("1A73E8"), spaceAfter=8)
    big = ParagraphStyle("big", parent=base, fontName=bold, fontSize=24, leading=29, spaceAfter=8)
    sub = ParagraphStyle("sub", parent=base, fontSize=11, leading=14, textColor=c("3C4043"), spaceAfter=16)
    right = ParagraphStyle("right", parent=cell_b, alignment=TA_RIGHT)

    page_w, page_h = A4
    margin = 16 * mm
    usable = page_w - 2 * margin
    story: list = []
    keep_alive: list = []   # BytesIO картинок должны жить до сборки документа

    def status_html(key: str, size: float | None = None) -> str:
        sz = f' size="{size}"' if size else ""
        return f'<font color="#{STATUS_COLORS[key]}"{sz}>●</font>&nbsp;<font name="{bold}" color="#{STATUS_COLORS[key]}"{sz}>' \
               f'{_x(STATUS_LABELS[key])}</font>'

    # --- Титул ------------------------------------------------------------------
    story.append(Spacer(1, 30 * mm))
    story.append(Paragraph("ФОТООТЧЁТ О ВЫПОЛНЕНИИ РАБОТ", kicker))
    story.append(Paragraph(_x(proj_name), big))
    tz_title = ((project.get("tz") or {}).get("title") or "").strip()
    if tz_title:
        story.append(Paragraph(f"по документу: {_x(tz_title)}", sub))
    info = []
    if project.get("customer"):
        info.append(("Заказчик", project["customer"]))
    if project.get("contractor"):
        info.append(("Исполнитель", project["contractor"]))
    if period_text(s):
        info.append(("Период съёмки", period_text(s)))
    info.append(("Дата отчёта", _dt.date.today().strftime("%d.%m.%Y")))
    if only_status_text(opts):
        info.append(("В отчёте пункты", only_status_text(opts)))
    t = Table([[Paragraph(_x(k), small), Paragraph(_x(v), cell_b)] for k, v in info],
              colWidths=[38 * mm, usable - 38 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [t, Spacer(1, 10 * mm), Paragraph("Сводка", h2)]

    rows = [[Paragraph("Пунктов к исполнению", cell_b), Paragraph(str(s["total"]), right)]]
    styles = []
    for key in ("done", "provided", "executed", "todo", "na"):
        if key == "na" and not s["counts"]["na"]:
            continue
        rows.append([Paragraph(status_html(key), cell), Paragraph(str(s["counts"][key]), right)])
        styles.append(("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), c(STATUS_TINTS[key])))
    if s["sections"]:
        rows.append([Paragraph("Разделов ТЗ (заголовков)", small), Paragraph(str(s["sections"]), right)])
    rows.append([Paragraph("Готовность", cell_b), Paragraph(f"{round(s['progress'] * 100)} %", right)])
    rows.append([Paragraph("Фото всего", cell), Paragraph(str(s["photos"]), right)])
    rows.append([Paragraph("в пунктах ТЗ / без пункта", small),
                 Paragraph(f"{s['photos_in_items']} / {s['photos_unsorted']}", right)])
    t = Table(rows, colWidths=[70 * mm, 28 * mm], hAlign="LEFT")
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, c("DADCE0")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)] + styles))
    story.append(t)
    # полоса готовности
    done_w = max(0.0, min(1.0, s["progress"])) * 98 * mm
    bar = Table([["", ""]], colWidths=[max(0.1, done_w), max(0.1, 98 * mm - done_w)], rowHeights=[3 * mm],
                hAlign="LEFT")
    bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), c(STATUS_COLORS["done"])),
                             ("BACKGROUND", (1, 0), (1, 0), c("E8EAED"))]))
    story += [Spacer(1, 3 * mm), bar, PageBreak()]

    # --- Таблица исполнения ------------------------------------------------------
    story.append(Paragraph("Таблица исполнения", h1))
    widths = [13 * mm, None, 18 * mm, 32 * mm, 20 * mm, 12.5 * mm]
    widths[1] = usable - sum(w for w in widths if w)
    data = [[Paragraph(h, cell_head) for h in ("№", "Пункт ТЗ", "Объём", "Статус", "Дата", "Фото")]]
    tstyle = [("BACKGROUND", (0, 0), (-1, 0), c("37474F")), ("GRID", (0, 0), (-1, -1), 0.4, c("DADCE0")),
              ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3),
              ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("LEFTPADDING", (0, 0), (-1, -1), 4),
              ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    center = ParagraphStyle("c", parent=cell, alignment=TA_CENTER)
    for r, it in enumerate(items, start=1):
        key = status_key(it)
        sec = sections.get(it.get("id"))
        if sec is not None:
            level = int(it.get("level") or 1)
            data.append([Paragraph(_x(it.get("num") or ""), cell_b),
                         Paragraph(_x(it.get("title") or ""), ParagraphStyle(
                             f"sec{level}", parent=cell_b, leftIndent=3.5 * mm * (level - 1))),
                         "", Paragraph(f"готово {sec[0]} из {sec[1]}" if sec[1] else "", small), "",
                         Paragraph("—", center)])
            tstyle.append(("BACKGROUND", (0, r), (-1, r), c("F8F9FA")))
            continue
        level = int(it.get("level") or 1)
        st = cell_b if level == 1 else cell
        indent = ParagraphStyle(f"ind{level}", parent=st, leftIndent=3.5 * mm * (level - 1))
        n = len(idx.for_item(it))
        data.append([Paragraph(_x(it.get("num") or ""), st), Paragraph(_x(it.get("title") or it.get("text") or ""), indent),
                     Paragraph(_x(qty_text(it)), cell), Paragraph(status_html(key, 7.6), cell),
                     Paragraph(_x(fmt_date(it.get("done_at"))), cell),
                     Paragraph(str(n) if n else "—", center)])
        tstyle.append(("BACKGROUND", (3, r), (3, r), c(STATUS_TINTS[key])))
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle(tstyle))
    story += [t, PageBreak(), Paragraph("Исполнение по пунктам ТЗ", h1)]

    # --- Пункты с фото -------------------------------------------------------------
    per_row = opts["photos_per_row"]
    counter = [0]

    def photo_table(photos: list[dict]):
        cells = []
        col_w = usable / per_row
        img_w_max = col_w - 4 * mm
        img_h_max = {1: 120 * mm, 2: 80 * mm, 3: 56 * mm, 4: 42 * mm}[per_row]
        for p in photos:
            img = cache.get(p)
            if not img:
                continue
            data_b, w, h = img
            counter[0] += 1
            scale = min(img_w_max / w, img_h_max / h)
            bio = io.BytesIO(data_b)
            keep_alive.append(bio)
            cells.append([Image(bio, width=w * scale, height=h * scale),
                          Spacer(1, 1.2 * mm), Paragraph(_x(photo_caption(counter[0], p)), caption)])
        if not cells:
            return None
        grid = [cells[i:i + per_row] for i in range(0, len(cells), per_row)]
        if len(grid[-1]) < per_row:
            grid[-1] += [""] * (per_row - len(grid[-1]))
        tb = Table(grid, colWidths=[col_w] * per_row)
        tb.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                                ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
        return tb

    for i, it in enumerate(items):
        key = status_key(it)
        photos = idx.for_item(it)
        level = int(it.get("level") or 1)
        head = Paragraph(_x(item_label(it) or "Пункт"), h2 if level <= 1 else h3)
        if it.get("id") in sections:
            story += [CondPageBreak(30 * mm), head]
            continue
        block = [head]
        text = (it.get("text") or "").strip()
        if text and not same_text(text, it.get("title") or ""):
            block.append(Paragraph(_x(text), muted))
        meta = [status_html(key)]
        if it.get("done_at"):
            meta.append(_x(fmt_date(it.get("done_at"))))
        if qty_text(it):
            meta.append("объём: " + _x(qty_text(it)))
        meta.append(f"фото: {len(photos)}")
        block.append(Paragraph('<font color="#5F6368">  ·  </font>'.join(meta),
                               ParagraphStyle("meta", parent=base, spaceBefore=2, spaceAfter=2)))
        if (it.get("comment") or "").strip():
            block.append(Paragraph(f'<font name="{bold}">Комментарий:</font> <i>{_x(it["comment"].strip())}</i>', muted))
        tb = photo_table(photos) if photos else None
        if tb is not None:
            # заголовок пункта не должен оставаться внизу страницы без фото
            story.append(CondPageBreak({1: 150, 2: 105, 3: 80, 4: 65}[per_row] * mm))
            story.append(KeepTogether(block + [Spacer(1, 2 * mm)]))
            story.append(tb)
        else:
            if key != "na":
                block.append(Paragraph("Фото не приложены." if not photos else "Файлы фото не найдены.", note))
            story.append(KeepTogether(block))

    if opts["include_unsorted"]:
        loose = idx.unsorted()
        if loose:
            story += [PageBreak(), Paragraph("Фото без пункта ТЗ", h1),
                      Paragraph(f"Фото, которые не отнесены ни к одному пункту: {len(loose)}.", small),
                      Spacer(1, 3 * mm)]
            tb = photo_table(loose)
            if tb is not None:
                story.append(tb)

    # --- Колонтитул «стр. N из M» ------------------------------------------------------
    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._saved: list = []

        def showPage(self):  # noqa: N802 — API reportlab
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                self.setFont(reg, 7.5)
                self.setFillColor(c("80868B"))
                self.setStrokeColor(c("DADCE0"))
                self.setLineWidth(0.5)
                self.line(margin, 11 * mm, page_w - margin, 11 * mm)
                name = proj_name if len(proj_name) <= 90 else proj_name[:89] + "…"
                self.drawString(margin, 7 * mm, name)
                self.drawRightString(page_w - margin, 7 * mm, f"стр. {self._pageNumber} из {total}")
                super().showPage()
            super().save()

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    doc = SimpleDocTemplate(out_path, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f"Фотоотчёт — {proj_name}", author="ФотоОтчёт",
                            subject=tz_title[:250])
    doc.build(story, canvasmaker=NumberedCanvas)
    keep_alive.clear()
    return out_path
