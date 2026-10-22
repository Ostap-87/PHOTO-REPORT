"""Чтение и запись фото: JPG/PNG/WEBP/TIFF/HEIC/HEIF, поворот по EXIF, метаданные (дата, GPS, камера)."""
from __future__ import annotations

import os
import struct
import tempfile
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

try:  # HEIC/HEIF с iPhone
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception:  # noqa: BLE001 — без pillow_heif остальные форматы работают
    HEIF_OK = False

SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif", ".bmp"}

# Теги EXIF
_ORIENTATION = 0x0112
_MAKE, _MODEL, _DATETIME = 0x010F, 0x0110, 0x0132
_EXIF_IFD, _GPS_IFD = 0x8769, 0x8825
_DT_ORIGINAL, _DT_DIGITIZED = 0x9003, 0x9004


def load_image(path: str | os.PathLike) -> tuple[np.ndarray, bytes | None, dict]:
    """Читает фото -> (rgb HxWx3 uint8, исходные EXIF-байты или None, meta).

    Картинка уже повёрнута по EXIF Orientation. meta: {"datetime": "YYYY-MM-DD HH:MM:SS" | "",
    "gps": [lat, lon] | None, "camera": "Apple iPhone 15" | "", "w": int, "h": int, "format": str,
    "orientation": int}.
    """
    path = Path(path)
    with Image.open(path) as im:
        fmt = (im.format or path.suffix.lstrip(".")).upper()
        try:
            im.seek(0)  # многостраничный TIFF / анимация — первый кадр
        except Exception:  # noqa: BLE001
            pass
        exif_bytes = im.info.get("exif") or None
        meta: dict = {"datetime": "", "gps": None, "camera": ""}
        orientation = 1
        try:  # битые метаданные не должны мешать открыть фото
            exif = im.getexif()
            # HEIC: pillow_heif сам поворачивает картинку и сбрасывает тег, исходный — в info
            orientation = int(im.info.get("original_orientation") or exif.get(_ORIENTATION, 1) or 1)
            meta = _meta_from_exif(exif)
        except Exception:  # noqa: BLE001
            pass
        im.load()
        try:
            im = ImageOps.exif_transpose(im)  # для HEIC pillow_heif уже повернул и сбросил тег — no-op
        except Exception:  # noqa: BLE001
            pass
        rgb = _to_rgb_array(im)
    h, w = rgb.shape[:2]
    meta.update({"w": int(w), "h": int(h), "format": fmt, "orientation": orientation})
    return rgb, (bytes(exif_bytes) if exif_bytes else None), meta


def _to_rgb_array(im: Image.Image) -> np.ndarray:
    """Любой режим PIL -> RGB uint8 (прозрачность — на белом фоне, 16 бит -> 8 бит)."""
    mode = im.mode
    if mode in ("I;16", "I;16B", "I;16L", "I;16N", "I"):
        a = np.asarray(im, dtype=np.float64)
        mx = 65535.0 if a.max() > 255 else 255.0
        g = np.clip(a / mx * 255.0 + 0.5, 0, 255).astype(np.uint8)
        return np.ascontiguousarray(np.stack([g] * 3, axis=-1))
    if mode == "F":
        a = np.asarray(im, dtype=np.float64)
        a = (a - a.min()) / max(1e-9, a.max() - a.min())
        g = (a * 255 + 0.5).astype(np.uint8)
        return np.ascontiguousarray(np.stack([g] * 3, axis=-1))
    if mode in ("RGBA", "LA", "PA") or (mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    return np.ascontiguousarray(np.asarray(im, dtype=np.uint8))


def _rational(v) -> float:
    try:
        if isinstance(v, tuple) and len(v) == 2:
            return float(v[0]) / float(v[1]) if float(v[1]) else 0.0
        return float(v)
    except Exception:  # noqa: BLE001
        return 0.0


def _dms(v) -> float | None:
    try:
        d, m, s = (_rational(x) for x in v)
        return d + m / 60.0 + s / 3600.0
    except Exception:  # noqa: BLE001
        return None


def _clean(s) -> str:
    if isinstance(s, bytes):
        s = s.decode("utf-8", "ignore")
    return str(s or "").replace("\x00", "").strip()


def _parse_dt(s: str) -> str:
    s = _clean(s)
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return ""


def _meta_from_exif(exif: Image.Exif) -> dict:
    meta: dict = {"datetime": "", "gps": None, "camera": ""}
    try:
        sub = exif.get_ifd(_EXIF_IFD)
    except Exception:  # noqa: BLE001
        sub = {}
    for v in (sub.get(_DT_ORIGINAL), sub.get(_DT_DIGITIZED), exif.get(_DATETIME)):
        if v:
            dt = _parse_dt(v)
            if dt:
                meta["datetime"] = dt
                break
    make, model = _clean(exif.get(_MAKE)), _clean(exif.get(_MODEL))
    if make and model.lower().startswith(make.lower().split()[0]):
        meta["camera"] = model          # «Canon» + «Canon EOS R6» -> «Canon EOS R6»
    else:
        meta["camera"] = " ".join(x for x in (make, model) if x)
    try:
        gps = exif.get_ifd(_GPS_IFD)
    except Exception:  # noqa: BLE001
        gps = {}
    if gps and 2 in gps and 4 in gps:
        lat, lon = _dms(gps[2]), _dms(gps[4])
        if lat is not None and lon is not None and (lat or lon):
            if _clean(gps.get(1, "N")).upper().startswith("S"):
                lat = -lat
            if _clean(gps.get(3, "E")).upper().startswith("W"):
                lon = -lon
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                meta["gps"] = [round(lat, 7), round(lon, 7)]
    return meta


def _reset_orientation(exif_bytes: bytes) -> bytes:
    """Ставит Orientation=1 прямо в байтах EXIF (остальное, включая MakerNote, не трогаем)."""
    prefix = b""
    data = exif_bytes
    if data.startswith(b"Exif\x00\x00"):
        prefix, data = data[:6], data[6:]
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return exif_bytes
    e = "<" if data[:2] == b"II" else ">"
    buf = bytearray(data)
    try:
        (ifd0,) = struct.unpack_from(e + "I", buf, 4)
        (n,) = struct.unpack_from(e + "H", buf, ifd0)
        for i in range(n):
            off = ifd0 + 2 + 12 * i
            tag, typ, cnt = struct.unpack_from(e + "HHI", buf, off)
            if tag == _ORIENTATION and typ == 3 and cnt == 1:  # SHORT, значение в первых 2 байтах поля
                struct.pack_into(e + "H", buf, off + 8, 1)
                break
    except struct.error:
        return exif_bytes
    return prefix + bytes(buf)


def _strip_big_exif(exif_bytes: bytes) -> bytes | None:
    """EXIF длиннее 64 КБ не влезает в JPEG: пересобираем без MakerNote и миниатюры."""
    try:
        ex = Image.Exif()
        ex.load(exif_bytes)
        sub = ex.get_ifd(_EXIF_IFD)
        sub.pop(0x927C, None)  # MakerNote
        ex[_ORIENTATION] = 1
        out = ex.tobytes()
        return out if len(out) < 65000 else None
    except Exception:  # noqa: BLE001
        return None


def save_jpeg(rgb: np.ndarray, path: str | os.PathLike, exif_bytes: bytes | None = None, quality: int = 92) -> None:
    """Сохраняет RGB uint8 в JPEG (атомарно). EXIF сохраняется, Orientation сбрасывается в 1."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.fromarray(np.ascontiguousarray(rgb[..., :3]).astype(np.uint8), "RGB")
    kw: dict = {"quality": int(quality), "optimize": False}
    if exif_bytes:
        ex = _reset_orientation(exif_bytes)
        if not ex.startswith(b"Exif\x00\x00"):
            ex = b"Exif\x00\x00" + ex
        if len(ex) >= 65000:
            ex = _strip_big_exif(ex)
        if ex:
            kw["exif"] = ex
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".jpg", dir=str(path.parent))
    os.close(fd)
    try:
        img.save(tmp, "JPEG", **kw)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def thumbnail(rgb: np.ndarray, max_side: int = 480) -> np.ndarray:
    """Уменьшенная копия с длинной стороной max_side (маленькие картинки не увеличиваем)."""
    h, w = rgb.shape[:2]
    s = max_side / max(h, w)
    if s >= 1:
        return rgb.copy()
    return cv2.resize(rgb, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)


def resize_max(rgb: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    """Уменьшает до max_side по длинной стороне; возвращает (картинка, масштаб <= 1)."""
    h, w = rgb.shape[:2]
    s = max_side / max(h, w)
    if s >= 1:
        return rgb, 1.0
    out = cv2.resize(rgb, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    return out, s
