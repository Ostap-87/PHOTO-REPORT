"""Отчёт PowerPoint (16:9): титул со сводкой, затем по слайду на пункт — номер и текст «в совершенном
виде», статус, комментарий и до 6 фото; больше фото — слайды «(продолжение)».

Раскладка слайдов считается один раз (slides_layout) и служит и для .pptx, и для картинок предпросмотра
(render_preview) — что в предпросмотре, то и в файле.
"""

from __future__ import annotations

import io
import os

from .common import (STATUS_COLORS, ImageCache, PhotoIndex, fmt_date, norm_options, photo_caption, select_items,
                     status_key, status_label, summary, text_of, title_of)

W_IN, H_IN = 13.333, 7.5                 # 16:9
PER_SLIDE = 6


def _grid(n: int) -> tuple[int, int]:
    return {1: (1, 1), 2: (2, 1), 3: (3, 1), 4: (2, 2)}.get(n, (3, 2))


def slides_layout(project: dict, project_dir: str, options: dict | None = None) -> list[dict]:
    """[{kind: "title"|"item", title, sub, text, status, photos: [(photo, (data, w, h), n, box)]}]; box — дюймы."""
    opts = norm_options(options)
    idx = PhotoIndex(project, opts["skip_duplicates"])
    cache = ImageCache(project_dir, min(opts["max_px"], 1600), opts["jpeg_quality"])
    s = summary(project)
    slides = [{"kind": "title", "title": f"Фотоотчёт: {project.get('name') or ''}".strip(": "),
               "sub": " · ".join(x for x in [project.get("customer") or "", (project.get("tz") or {}).get("title") or ""] if x),
               "text": f"Пунктов выполнено: {s.get('complete', 0)} из {s.get('total', 0)} · фото в отчёте: {s.get('photos_in_items', 0)}",
               "photos": []}]
    counter = 0
    for it in select_items(project, opts):
        photos = []
        for ph in idx.for_item(it):
            img = cache.get(ph)
            if img:
                counter += 1
                photos.append((ph, img, counter))
        key = status_key(it)
        if not photos and key == "todo" and not (it.get("comment") or "").strip():
            continue
        head = f"{(it.get('num') or '').strip()} {title_of(it)}".strip()
        sub = status_label(key) + (f" · {fmt_date(it['done_at'])}" if it.get("done_at") and key != "todo" else "")
        if (it.get("comment") or "").strip():
            sub += " — " + it["comment"].strip()
        text = text_of(it)
        text = "" if text.strip() == title_of(it).strip() else text
        chunks = [photos[i:i + PER_SLIDE] for i in range(0, len(photos), PER_SLIDE)] or [[]]
        for ci, chunk in enumerate(chunks):
            top = 2.15 if ci == 0 and text else 1.55
            cols, rows = _grid(len(chunk)) if chunk else (1, 1)
            cell_w, cell_h = (W_IN - 1.0) / cols, (H_IN - top - 0.35) / rows
            placed = []
            for k, (ph, (data, w, h), n) in enumerate(chunk):
                r, c = divmod(k, cols)
                bw, bh = cell_w - 0.2, cell_h - 0.45
                scale = min(bw / max(w, 1), bh / max(h, 1))
                iw, ih = w * scale, h * scale
                x = 0.5 + c * cell_w + (cell_w - iw) / 2
                y = top + r * cell_h + (bh - ih) / 2
                placed.append((ph, (data, w, h), n, (x, y, iw, ih), (0.5 + c * cell_w, y + ih + 0.04, cell_w)))
            slides.append({"kind": "item", "title": head + (" (продолжение)" if ci else ""), "sub": sub,
                           "text": text if ci == 0 else "", "status": key, "photos": placed})
    return slides


def export_pptx(project: dict, project_dir: str, out_path: str, options: dict | None = None) -> str:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(W_IN), Inches(H_IN)
    blank = prs.slide_layouts[6]

    def text(slide, x, y, w, h, s, size, bold=False, color="202124"):
        tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        r = p.add_run()
        r.text = s
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.color.rgb = RGBColor.from_string(color)
        return tb

    for sl in slides_layout(project, project_dir, options):
        slide = prs.slides.add_slide(blank)
        if sl["kind"] == "title":
            text(slide, 0.8, 2.3, W_IN - 1.6, 1.4, sl["title"], 36, True)
            if sl["sub"]:
                text(slide, 0.8, 3.7, W_IN - 1.6, 0.8, sl["sub"], 18, False, "5F6368")
            text(slide, 0.8, 4.5, W_IN - 1.6, 0.6, sl["text"], 16, False, "1F5F4A")
            continue
        text(slide, 0.5, 0.3, W_IN - 1.0, 0.8, sl["title"], 22, True)
        text(slide, 0.5, 1.0, W_IN - 1.0, 0.5, sl["sub"], 13, True, STATUS_COLORS[sl["status"]])
        if sl["text"]:
            text(slide, 0.5, 1.45, W_IN - 1.0, 0.7, sl["text"][:600], 11, False, "3C4043")
        for ph, (data, _w, _h), n, (x, y, iw, ih), (cx, cy, cw) in sl["photos"]:
            slide.shapes.add_picture(io.BytesIO(data), Inches(x), Inches(y), Inches(iw), Inches(ih))
            text(slide, cx, cy, cw, 0.3, photo_caption(n, ph), 9, False, "80868B")
    cp = prs.core_properties
    cp.title = f"Фотоотчёт — {project.get('name') or ''}".strip(" —")
    cp.author = "ФотоОтчёт"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    prs.save(out_path)
    return out_path


def render_preview(project: dict, project_dir: str, out_dir: str, options: dict | None = None,
                   limit: int = 12, px: int = 960) -> list[str]:
    """Картинки слайдов для предпросмотра (PNG), по той же раскладке, что export_pptx."""
    from PIL import Image, ImageDraw, ImageFont

    from .pdf_report import find_font

    os.makedirs(out_dir, exist_ok=True)
    k = px / W_IN
    ff = find_font() or (None, None)
    reg, bold = ff[0], ff[1] or ff[0]

    def font(path, size_pt):
        try:
            return ImageFont.truetype(path, max(8, int(size_pt / 72 * k))) if path else ImageFont.load_default()
        except OSError:
            return ImageFont.load_default()

    def wrapped(d, xy, s, fnt, maxw, lines, fill):
        out, cur = [], ""
        for w in (s or "").split():
            nxt = (cur + " " + w).strip()
            if d.textlength(nxt, font=fnt) <= maxw:
                cur = nxt
            else:
                out.append(cur)
                cur = w
        out.append(cur)
        if len(out) > lines:
            out = out[:lines]
            out[-1] = out[-1].rstrip(" ,.;:") + "…"
        y = xy[1]
        for line in out:
            d.text((xy[0], y), line, font=fnt, fill=fill)
            y += fnt.size * 1.25 if hasattr(fnt, "size") else 14

    def hexrgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    out = []
    for i, sl in enumerate(slides_layout(project, project_dir, options)[:limit]):
        im = Image.new("RGB", (px, int(H_IN * k)), "white")
        d = ImageDraw.Draw(im)
        if sl["kind"] == "title":
            d.text((0.8 * k, 2.3 * k), sl["title"], font=font(bold, 36), fill=(32, 33, 36))
            d.text((0.8 * k, 3.7 * k), sl["sub"], font=font(reg, 18), fill=(95, 99, 104))
            d.text((0.8 * k, 4.5 * k), sl["text"], font=font(reg, 16), fill=(31, 95, 74))
        else:
            wrapped(d, (0.5 * k, 0.3 * k), sl["title"], font(bold, 22), (W_IN - 1) * k, 2, (32, 33, 36))
            wrapped(d, (0.5 * k, 1.0 * k), sl["sub"], font(bold, 13), (W_IN - 1) * k, 1, hexrgb(STATUS_COLORS[sl["status"]]))
            if sl["text"]:
                wrapped(d, (0.5 * k, 1.45 * k), sl["text"], font(reg, 11), (W_IN - 1) * k, 3, (60, 64, 67))
            for ph, (data, _w, _h), n, (x, y, iw, ih), (cx, cy, cw) in sl["photos"]:
                with Image.open(io.BytesIO(data)) as p:
                    im.paste(p.convert("RGB").resize((max(1, int(iw * k)), max(1, int(ih * k)))), (int(x * k), int(y * k)))
                d.text((cx * k, cy * k), photo_caption(n, ph), font=font(reg, 9), fill=(128, 134, 139))
        d.rectangle((0, 0, im.width - 1, im.height - 1), outline=(220, 224, 220))
        path = os.path.join(out_dir, f"slide-{i + 1:02d}.png")
        im.save(path)
        out.append(path)
    return out
