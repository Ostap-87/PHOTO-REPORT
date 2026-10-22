"""Разбор ТЗ из PDF (PyMuPDF).

Для левой панели отдаём картинки страниц (PNG ~110 dpi). Пункты берём из текстового
слоя: строки склеиваются в абзацы (перенос строки внутри пункта), колонтитулы и номера
страниц отбрасываются, таблицы с линиями распознаются через find_tables().
У пунктов из PDF source = {"kind": "pdf"|"row", "page": N (с 1), "ref": None,
"bbox": [x0, y0, x1, y1] в долях страницы} — по bbox можно подсветить место на картинке.
Если текста нет (скан), возвращаются только страницы и пустой список пунктов (OCR не делаем).
"""

from __future__ import annotations

import re
from collections import Counter

from .common import Para, TableBlock, build_items, classify_marker, clean_text, one_line, title_from_filename

PAGE_DPI = 110
MAX_PAGES = 300
_PAGE_NUM_RE = re.compile(r"^(стр\.?|страница|page|лист)?\s*\d{1,4}\s*((из|/|of)\s*\d{1,4})?$", re.I)


def _open(data: bytes):
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        raise ValueError("Не удалось открыть PDF: файл повреждён или это не PDF") from e
    try:
        pymupdf.no_recommend_layout()  # без рекламы pymupdf_layout в журнале сервера
    except AttributeError:
        pass
    if doc.needs_pass and not doc.authenticate(""):
        raise ValueError("PDF защищён паролем — снимите защиту и загрузите снова")
    return doc


def _raw_lines(page) -> list[dict]:
    """Строки страницы: фрагменты одной визуальной строки склеены слева направо."""
    d = page.get_text("dict")
    width = page.rect.width
    frags = []
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            dx, dy = ln.get("dir", (1, 0))
            if abs(dy) > 0.2:
                continue  # вертикальный текст (штампы, водяные знаки)
            x0, y0, x1, y1 = ln["bbox"]
            frags.append({
                "text": "".join(s["text"] for s in ln["spans"]),
                "bold": all((s.get("flags", 0) & 16) or "bold" in s.get("font", "").lower() for s in spans),
                "size": max(s.get("size", 0) for s in spans),
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "yc": (y0 + y1) / 2, "h": max(1.0, y1 - y0),
            })
    frags.sort(key=lambda f: f["yc"])
    rows: list[list[dict]] = []
    for f in frags:
        if rows:
            last = rows[-1]
            ref = sum(x["yc"] for x in last) / len(last)
            if abs(f["yc"] - ref) < 0.45 * min(f["h"], min(x["h"] for x in last)):
                last.append(f)
                continue
        rows.append([f])
    lines = []
    for row in rows:
        row.sort(key=lambda f: f["x0"])
        seg = [row[0]]
        segs = [seg]
        for f in row[1:]:
            if f["x0"] - seg[-1]["x1"] > 0.25 * width:
                seg = [f]
                segs.append(seg)
            else:
                seg.append(f)
        for seg in segs:
            text = " ".join(one_line(f["text"]) for f in seg)
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                continue
            lines.append({
                "text": text,
                "bold": all(f["bold"] for f in seg),
                "size": max(f["size"] for f in seg),
                "x0": min(f["x0"] for f in seg), "x1": max(f["x1"] for f in seg),
                "y0": min(f["y0"] for f in seg), "y1": max(f["y1"] for f in seg),
                "h": max(f["h"] for f in seg),
            })
    return lines


def _tables(page) -> list:
    try:
        found = page.find_tables()
    except Exception:  # noqa: BLE001 — старые версии PyMuPDF или нестандартная разметка
        return []
    out = []
    for t in getattr(found, "tables", []):
        try:
            if t.row_count >= 2 and t.col_count >= 2:
                out.append(t)
        except Exception:  # noqa: BLE001
            continue
    return out


def _inside(line: dict, bbox) -> bool:
    cx = (line["x0"] + line["x1"]) / 2
    cy = (line["y0"] + line["y1"]) / 2
    return bbox[0] - 1 <= cx <= bbox[2] + 1 and bbox[1] - 1 <= cy <= bbox[3] + 1


def parse_pdf(data: bytes, filename: str) -> dict:
    """Разбор PDF → словарь parse_tz (html = None, pages = PNG страниц)."""
    doc = _open(data)
    warnings: list[str] = []
    pages: list[bytes] = []
    n_pages = doc.page_count
    if n_pages > MAX_PAGES:
        warnings.append(f"В PDF {n_pages} стр.; показаны первые {MAX_PAGES}.")
    page_lines: list[list[dict]] = []
    page_tables: list[list] = []
    sizes: list[tuple[float, float]] = []
    empty_pages = []
    page_texts: list[str] = []
    for pno in range(min(n_pages, MAX_PAGES)):
        page = doc[pno]
        pages.append(page.get_pixmap(dpi=PAGE_DPI).tobytes("png"))
        try:
            ptext = page.get_text("text", sort=True)
        except Exception:  # noqa: BLE001
            ptext = page.get_text("text")
        lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in ptext.splitlines()]
        ptext = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        page_texts.append(f"--- стр. {pno + 1} ---\n{ptext}".rstrip())
        sizes.append((page.rect.width, page.rect.height))
        lines = _raw_lines(page)
        if not lines and page.get_images():
            empty_pages.append(pno + 1)
        tabs = _tables(page) if lines else []
        page_lines.append(lines)
        page_tables.append(tabs)

    full_text = "\n\n".join(page_texts)
    total_chars = sum(len(ln["text"].replace(" ", "")) for pl in page_lines for ln in pl)
    title_guess = title_from_filename(filename)
    meta_title = one_line((doc.metadata or {}).get("title", ""))
    if meta_title and not re.match(r"^(microsoft|untitled|без названия|документ|document)", meta_title, re.I):
        title_guess = meta_title
    if total_chars < 30:
        doc.close()
        warnings.append("Похоже на скан: в PDF нет текстового слоя. Пункты ТЗ нужно добавить вручную.")
        return {"title": title_guess, "kind": "pdf", "items": [], "html": None, "pages": pages,
                "warnings": warnings, "full_text": full_text}
    if empty_pages:
        warnings.append("Страницы без текста (сканы): " + ", ".join(map(str, empty_pages))
                        + " — пункты с них нужно добавить вручную.")

    # Колонтитулы: номера страниц и строки, повторяющиеся на нескольких страницах у краёв.
    edge_counts: Counter = Counter()
    for pno, lines in enumerate(page_lines):
        H = sizes[pno][1]
        for ln in lines:
            if ln["y1"] < H * 0.08 or ln["y0"] > H * 0.92:
                edge_counts[re.sub(r"\d+", "#", ln["text"])] += 1
    body_sizes: Counter = Counter()
    for lines in page_lines:
        for ln in lines:
            body_sizes[round(ln["size"])] += len(ln["text"])
    body_size = body_sizes.most_common(1)[0][0] if body_sizes else 10

    elements: list[tuple[int, float, object]] = []  # (страница, y, строка или таблица)
    for pno, lines in enumerate(page_lines):
        W, H = sizes[pno]
        bboxes = [t.bbox for t in page_tables[pno]]
        for ln in lines:
            at_edge = ln["y1"] < H * 0.08 or ln["y0"] > H * 0.92
            if at_edge and (_PAGE_NUM_RE.match(ln["text"]) or
                            (len(page_lines) > 1 and edge_counts[re.sub(r"\d+", "#", ln["text"])] >= 2)):
                continue
            if any(_inside(ln, bb) for bb in bboxes):
                continue
            ln["page"] = pno
            elements.append((pno, ln["y0"], ln))
        for t in page_tables[pno]:
            elements.append((pno, t.bbox[1], t))
    elements.sort(key=lambda e: (e[0], e[1]))

    left = {}
    right = {}
    for pno, lines in enumerate(page_lines):
        xs0 = [ln["x0"] for ln in lines] or [0]
        xs1 = [ln["x1"] for ln in lines] or [sizes[pno][0]]
        left[pno] = min(xs0)
        right[pno] = max(xs1)

    stream: list = []
    para: dict | None = None
    p_index = 0
    t_index = 0

    def close() -> None:
        nonlocal para, p_index
        if para is None:
            return
        pno = para["page"]
        W, H = sizes[pno]
        x0, y0, x1, y1 = para["bbox"]
        text_w = right[pno] - left[pno] or W
        centered = (abs((x0 + x1) / 2 - (left[pno] + right[pno]) / 2) < 0.06 * text_w
                    and x0 > left[pno] + 0.08 * text_w)
        heading = 1 if (para["size"] >= body_size * 1.2 and len(para["text"]) <= 150) else 0
        src = {"kind": "pdf", "index": p_index, "page": pno + 1, "ref": None,
               "bbox": [round(x0 / W, 4), round(y0 / H, 4), round(x1 / W, 4), round(y1 / H, 4)]}
        stream.append(Para(text=clean_text(para["text"]), src=src, bold=para["bold"], heading=heading,
                           centered=centered))
        p_index += 1
        para = None

    for pno, _y, el in elements:
        if not isinstance(el, dict):
            close()
            rows, srcs, merged = [], [], []
            W, H = sizes[pno]
            try:
                extracted = el.extract()
            except Exception:  # noqa: BLE001
                continue
            for ri, row in enumerate(extracted):
                cells = [clean_text(c) if c else "" for c in row]
                rows.append(cells)
                try:
                    rb = el.rows[ri].bbox
                    bbox = [round(rb[0] / W, 4), round(rb[1] / H, 4), round(rb[2] / W, 4), round(rb[3] / H, 4)]
                except Exception:  # noqa: BLE001
                    bbox = None
                srcs.append({"kind": "row", "index": ri, "table": t_index, "row": ri, "page": pno + 1,
                             "ref": None, "bbox": bbox})
                merged.append(sum(1 for c in row if c) == 1 and sum(1 for c in row if c is None) >= 1)
            stream.append(TableBlock(rows=rows, srcs=srcs, merged=merged))
            t_index += 1
            continue
        ln = el
        if para is not None:
            prev = para["last"]
            same_page = prev["page"] == ln["page"]
            text_w = right[ln["page"]] - left[ln["page"]] or 1
            gap = ln["y0"] - prev["y1"] if same_page else 0
            short_prev = prev["x1"] < right[prev["page"]] - 0.15 * text_w
            prev_offset = prev["x0"] > left[prev["page"]] + 0.3 * text_w
            mk = classify_marker(ln["text"])
            # Строка, начинающаяся с тире, после незаконченной строки — перенос, а не новый пункт.
            dash_wrap = (mk is not None and mk.kind == "bullet" and ln["text"][:1] in "—–-"
                         and not prev["text"].rstrip().endswith((".", ";", ":", "!", "?")))
            cont = ((mk is None or dash_wrap)
                    and ln["bold"] == prev["bold"]
                    and abs(ln["size"] - prev["size"]) <= 1.0
                    and gap <= 0.7 * max(ln["h"], prev["h"])
                    and not short_prev
                    and not prev_offset)
            if cont:
                para["text"] += " " + ln["text"]
                b = para["bbox"]
                if same_page:
                    para["bbox"] = [min(b[0], ln["x0"]), min(b[1], ln["y0"]), max(b[2], ln["x1"]), max(b[3], ln["y1"])]
                para["last"] = ln
                continue
            close()
        para = {"text": ln["text"], "bold": ln["bold"], "size": ln["size"], "page": ln["page"],
                "bbox": [ln["x0"], ln["y0"], ln["x1"], ln["y1"]], "last": ln}
    close()
    doc.close()

    title, items = build_items(stream, title_guess)
    if not items:
        warnings.append("В PDF не найдено пунктов: добавьте их вручную.")
    return {"title": title, "kind": "pdf", "items": items, "html": None, "pages": pages, "warnings": warnings,
            "full_text": full_text}
