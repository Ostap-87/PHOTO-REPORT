# -*- coding: utf-8 -*-
"""Иконка приложения ФотоОтчёт для Mac: assets/PhotoReport.icns (+ assets/icon-1024.png).

    python3 tools/make_icon.py

Форма — как у иконок macOS (скруглённый квадрат 824 из 1024 с тенью), цвет — --accent интерфейса
(#1f5f4a): фотография (горы и солнце) поверх листа ТЗ и отметка «выполнено».
"""
import os

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "assets")
S = 2048


def _grad(top, bottom):
    g = Image.new("RGB", (1, 256))
    for y in range(256):
        t = y / 255
        g.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    return g.resize((S, S))


def icon():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    box = (200, 200, S - 200, S - 200)
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((box[0], box[1] + 24, box[2], box[3] + 24), radius=370,
                                             fill=(0, 0, 0, 90))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(28)))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=370, fill=255)
    img.paste(_grad((46, 128, 98), (24, 82, 62)), (0, 0), mask)
    d = ImageDraw.Draw(img)
    # лист ТЗ сзади
    d.rounded_rectangle((760, 470, 1500, 1420), radius=40, fill=(226, 238, 232))
    for i in range(4):
        d.rounded_rectangle((860, 560 + i * 90, 1400 - i * 60, 600 + i * 90), radius=20, fill=(160, 196, 180))
    # фотография спереди
    x0, y0, x1, y1 = 520, 860, 1360, 1560
    d.rounded_rectangle((x0, y0, x1, y1), radius=36, fill=(255, 255, 255))
    ix0, iy0, ix1, iy1 = x0 + 50, y0 + 50, x1 - 50, y1 - 50
    d.rectangle((ix0, iy0, ix1, iy1), fill=(214, 233, 246))
    d.ellipse((ix1 - 250, iy0 + 70, ix1 - 110, iy0 + 210), fill=(255, 196, 61))
    d.polygon([(ix0, iy1), (ix0 + 260, iy0 + 260), (ix0 + 470, iy1)], fill=(46, 128, 98))
    d.polygon([(ix0 + 300, iy1), (ix0 + 560, iy0 + 330), (ix1, iy1)], fill=(24, 82, 62))
    # отметка «выполнено»
    cx, cy, r = 1440, 1520, 260
    d.ellipse((cx - r - 24, cy - r - 24, cx + r + 24, cy + r + 24), fill=(24, 82, 62))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255))
    d.line([(cx - 130, cy + 10), (cx - 35, cy + 105), (cx + 140, cy - 95)], fill=(31, 95, 74), width=70,
           joint="curve")
    return img.resize((1024, 1024), Image.LANCZOS)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    im = icon()
    im.save(os.path.join(OUT, "icon-1024.png"))
    im.save(os.path.join(OUT, "PhotoReport.icns"),
            sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])
    print("Иконка:", os.path.join(OUT, "PhotoReport.icns"))
