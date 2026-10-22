"""Общие помощники экспорта: статусы, выбор пунктов и фото, подготовка картинок, подписи."""

from __future__ import annotations

import datetime as _dt
import glob
import io
import os
import re

STATUS_ORDER = ["todo", "done", "provided", "executed", "na"]
STATUS_LABELS = {
    "todo": "Не начато",
    "done": "Выполнено",
    "provided": "Предоставлено",
    "executed": "Исполнено",
    "na": "Не требуется",
}
# Основной цвет статуса (текст, маркер) и светлая подложка (ячейки таблиц).
STATUS_COLORS = {"todo": "80868B", "done": "1E8E3E", "provided": "1A73E8", "executed": "8430CE", "na": "9AA0A6"}
STATUS_TINTS = {"todo": "F1F3F4", "done": "E6F4EA", "provided": "E8F0FE", "executed": "F3E8FD", "na": "F8F9FA"}
COMPLETE = ("done", "provided", "executed")

QUALITY_FLAGS = {
    "dark": "тёмное", "bright": "пересвечено", "overexposed": "пересвечено", "underexposed": "тёмное",
    "blurry": "нерезкое", "blur": "нерезкое", "noisy": "шумы", "noise": "шумы", "low_contrast": "низкий контраст",
    "lowcontrast": "низкий контраст", "small": "маленькое", "tilted": "завалено", "reshoot": "переснять",
}

DEFAULT_OPTIONS = {
    "photos_per_row": 2,
    "include_unsorted": False,
    "only_status": None,
    "max_px": 1600,        # длинная сторона фото в отчёте
    "jpeg_quality": 85,
    "skip_duplicates": False,
}


def norm_options(options: dict | None) -> dict:
    """Опции отчёта с значениями по умолчанию и проверкой границ."""
    o = dict(DEFAULT_OPTIONS)
    o.update({k: v for k, v in (options or {}).items() if v is not None or k == "only_status"})
    try:
        o["photos_per_row"] = max(1, min(4, int(o.get("photos_per_row") or 2)))
    except (TypeError, ValueError):
        o["photos_per_row"] = 2
    try:
        o["max_px"] = max(320, min(4000, int(o.get("max_px") or 1600)))
    except (TypeError, ValueError):
        o["max_px"] = 1600
    try:
        o["jpeg_quality"] = max(40, min(95, int(o.get("jpeg_quality") or 85)))
    except (TypeError, ValueError):
        o["jpeg_quality"] = 85
    st = o.get("only_status")
    if isinstance(st, str):
        st = [s.strip() for s in st.split(",") if s.strip()]
    o["only_status"] = set(st) if st else None
    o["include_unsorted"] = bool(o.get("include_unsorted"))
    return o


def status_key(item: dict) -> str:
    s = (item.get("status") or "todo").strip()
    return s if s in STATUS_LABELS else "todo"


def status_label(item_or_key) -> str:
    key = item_or_key if isinstance(item_or_key, str) else status_key(item_or_key)
    return STATUS_LABELS.get(key, STATUS_LABELS["todo"])


def select_items(project: dict, opts: dict) -> list[dict]:
    items = list(project.get("items") or [])
    if opts.get("only_status"):
        items = [it for it in items if status_key(it) in opts["only_status"]]
    return items


def has_children(items: list[dict], i: int) -> bool:
    return i + 1 < len(items) and int(items[i + 1].get("level") or 1) > int(items[i].get("level") or 1)


def same_text(a: str, b: str) -> bool:
    """Тексты совпадают без учёта регистра, пробелов и знаков в конце (полный текст = названию)."""
    def norm(t: str) -> str:
        return re.sub(r"\s+", " ", t or "").strip().rstrip(" .;:,").lower()
    return norm(a) == norm(b)


def item_label(item: dict) -> str:
    return f"{(item.get('num') or '').strip()} {item.get('title') or ''}".strip()


# ---------------------------------------------------------------------------
# Числа и даты
# ---------------------------------------------------------------------------

def fmt_number(v) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        f = float(v)
        if f.is_integer():
            s = f"{int(f):,}".replace(",", " ")
        else:
            s = f"{f:,.3f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")
        return s
    return str(v)


def qty_text(item: dict) -> str:
    q = fmt_number(item.get("qty"))
    u = (item.get("unit") or "").strip()
    return f"{q} {u}".strip() if q else u


def parse_dt(s) -> _dt.datetime | None:
    """Дата/время из строк «2026-10-21 14:03:11», «2026:10:21 14:03:11», ISO, «21.10.2026»."""
    if not s:
        return None
    if isinstance(s, _dt.datetime):
        return s
    if isinstance(s, _dt.date):
        return _dt.datetime(s.year, s.month, s.day)
    t = str(s).strip()
    m = re.match(r"^(\d{4})[-:.](\d{2})[-:.](\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?", t)
    if m:
        y, mo, d, h, mi, se = m.groups()
        try:
            return _dt.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(se or 0))
        except ValueError:
            return None
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{2}):(\d{2}))?", t)
    if m:
        d, mo, y, h, mi = m.groups()
        try:
            return _dt.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0))
        except ValueError:
            return None
    return None


def fmt_date(s) -> str:
    d = parse_dt(s)
    return d.strftime("%d.%m.%Y") if d else (str(s) if s else "")


def fmt_datetime(s) -> str:
    d = parse_dt(s)
    if not d:
        return str(s) if s else ""
    return d.strftime("%d.%m.%Y %H:%M") if (d.hour or d.minute) else d.strftime("%d.%m.%Y")


def photo_datetime(photo: dict):
    return parse_dt((photo.get("exif") or {}).get("datetime"))


# ---------------------------------------------------------------------------
# Фото
# ---------------------------------------------------------------------------

class PhotoIndex:
    """Фото и файлы проекта: по пунктам (фото — в порядке item.photo_ids) и «Неразобранное»."""

    def __init__(self, project: dict, skip_duplicates: bool = False):
        self.photos = [p for p in (project.get("photos") or []) if p.get("id")]
        self.by_id = {p["id"]: p for p in self.photos}
        self.item_ids = {it.get("id") for it in (project.get("items") or [])}
        self.skip_duplicates = skip_duplicates
        self.files = [f for f in (project.get("files") or []) if isinstance(f, dict) and f.get("id")]

    def files_for(self, item: dict) -> list[dict]:
        """Вложения пункта (не фото: pdf, docx, xlsx, видео…) в порядке добавления."""
        res = [f for f in self.files if item.get("id") and f.get("item_id") == item.get("id")]
        res.sort(key=lambda f: (str(f.get("added") or ""), f.get("name") or ""))
        return res

    def unsorted_files(self) -> list[dict]:
        res = [f for f in self.files if not f.get("item_id") or f.get("item_id") not in self.item_ids]
        res.sort(key=lambda f: (str(f.get("added") or ""), f.get("name") or ""))
        return res

    def _ok(self, p: dict) -> bool:
        return not (self.skip_duplicates and p.get("duplicate_of"))

    def for_item(self, item: dict) -> list[dict]:
        out, seen = [], set()
        for pid in item.get("photo_ids") or []:
            p = self.by_id.get(pid)
            if p is not None and pid not in seen and self._ok(p):
                out.append(p)
                seen.add(pid)
        extra = [p for p in self.photos if p.get("item_id") == item.get("id") and p["id"] not in seen and self._ok(p)]
        extra.sort(key=lambda p: (photo_datetime(p) or _dt.datetime.max, p.get("name") or ""))
        return out + extra

    def unsorted(self) -> list[dict]:
        res = [p for p in self.photos
               if (not p.get("item_id") or p.get("item_id") not in self.item_ids) and self._ok(p)]
        res.sort(key=lambda p: (photo_datetime(p) or _dt.datetime.max, p.get("name") or ""))
        return res


def photo_path(project_dir: str, photo: dict, which: str = "work") -> str | None:
    """Путь к файлу фото: рабочая копия → исходник → превью (или исходник → рабочая)."""
    pid = photo.get("id") or ""
    base = os.path.join(project_dir, "photos")
    ext = photo.get("ext") or os.path.splitext(photo.get("name") or "")[1]
    work = os.path.join(base, "work", f"{pid}.jpg")
    orig = os.path.join(base, "original", f"{pid}{ext}") if ext else ""
    thumb = os.path.join(base, "thumb", f"{pid}.jpg")
    cands = [work, orig, thumb] if which != "original" else [orig, work]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    if which == "original" or not orig:
        found = sorted(glob.glob(os.path.join(glob.escape(os.path.join(base, "original")), glob.escape(pid) + ".*")))
        if found:
            return found[0]
    return None


def file_path(project_dir: str, f: dict) -> str | None:
    """Путь к вложению: photos/files/<id><ext>; None — файла нет."""
    fid = f.get("id") or ""
    base = os.path.join(project_dir, "photos", "files")
    ext = f.get("ext") or os.path.splitext(f.get("name") or "")[1]
    cand = os.path.join(base, f"{fid}{ext}")
    if fid and os.path.isfile(cand):
        return cand
    found = sorted(glob.glob(os.path.join(glob.escape(base), glob.escape(fid) + ".*"))) if fid else []
    return found[0] if found else None


def fmt_size(n) -> str:
    """Размер файла по-русски: «820 Б», «120 КБ», «3,4 МБ»."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            if unit in ("Б", "КБ") or n >= 100:
                return f"{n:.0f} {unit}"
            return f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return ""


def file_label(f: dict) -> str:
    name = f.get("name") or f.get("id") or "файл"
    size = fmt_size(f.get("size")) if f.get("size") else ""
    return f"{name} ({size})" if size else name


def item_links(item: dict) -> list[str]:
    """item.links → список непустых строк (принимает и одну строку)."""
    links = item.get("links") or []
    if isinstance(links, str):
        links = [links]
    return [str(u).strip() for u in links if str(u or "").strip()]


def is_web_link(url: str) -> bool:
    """Ссылку можно сделать кликабельной (только http, https, mailto)."""
    return bool(re.match(r"^(https?://|mailto:)[^\s<>\"']+$", url or "", re.I))


def item_extra(item: dict) -> dict:
    """Необязательные поля пункта из анализа ТЗ (пустые — не показываем)."""
    def txt(key: str) -> str:
        v = item.get(key)
        return str(v).strip() if v not in (None, "") else ""
    return {"section": txt("section"), "required": txt("required"), "note": txt("note"), "links": item_links(item)}


_HEIF_READY: bool | None = None


def _ensure_heif() -> None:
    global _HEIF_READY
    if _HEIF_READY is None:
        try:
            import pillow_heif  # type: ignore

            pillow_heif.register_heif_opener()
            _HEIF_READY = True
        except Exception:  # noqa: BLE001 — pillow-heif не обязателен
            _HEIF_READY = False


class ImageCache:
    """Уменьшенные JPEG для вставки в отчёт (≤ max_px по длинной стороне, качество quality)."""

    def __init__(self, project_dir: str, max_px: int = 1600, quality: int = 85):
        self.project_dir = project_dir
        self.max_px = max_px
        self.quality = quality
        self._cache: dict[str, tuple[bytes, int, int] | None] = {}

    def get(self, photo: dict) -> tuple[bytes, int, int] | None:
        """JPEG для отчёта: рабочая копия, иначе исходник, иначе превью; None — файла нет или он битый."""
        pid = photo.get("id") or ""
        if pid in self._cache:
            return self._cache[pid]
        cands = [photo_path(self.project_dir, photo, "work"),
                 os.path.join(self.project_dir, "photos", "thumb", f"{pid}.jpg")]
        res = None
        for path in dict.fromkeys(c for c in cands if c and os.path.isfile(c)):
            res = self._prepare(path)
            if res:
                break
        self._cache[pid] = res
        return res

    def _prepare(self, path: str) -> tuple[bytes, int, int] | None:
        from PIL import Image, ImageOps

        if path.lower().endswith((".heic", ".heif")):
            _ensure_heif()
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                if im.mode not in ("RGB", "L"):
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    if im.mode in ("RGBA", "LA", "P"):
                        im = im.convert("RGBA")
                        bg.paste(im, mask=im.split()[-1])
                    else:
                        bg.paste(im.convert("RGB"))
                    im = bg
                elif im.mode == "L":
                    im = im.convert("RGB")
                im.thumbnail((self.max_px, self.max_px), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=self.quality, optimize=True, progressive=False)
                return buf.getvalue(), im.width, im.height
        except Exception:  # noqa: BLE001 — битый или неподдерживаемый файл пропускаем
            return None


def photo_caption(n: int, photo: dict) -> str:
    """«Фото 12 · 21.10.2026 14:03 · улучшено · лица скрыты»."""
    parts = [f"Фото {n}"]
    dt = fmt_datetime((photo.get("exif") or {}).get("datetime"))
    if dt:
        parts.append(dt)
    if photo.get("enhanced"):
        parts.append("улучшено")
    if photo.get("faces_blurred"):
        parts.append("лица скрыты")
    return " · ".join(parts)


def quality_text(photo: dict) -> str:
    q = photo.get("quality") or {}
    flags = [QUALITY_FLAGS.get(str(f).lower(), str(f)) for f in (q.get("flags") or [])]
    if q.get("reshoot") and "переснять" not in flags:
        flags.append("переснять")
    return ", ".join(dict.fromkeys(flags)) if flags else ("хорошее" if q else "")


# ---------------------------------------------------------------------------
# Сводка
# ---------------------------------------------------------------------------

def _level(item: dict) -> int:
    try:
        return int(item.get("level") or 1)
    except (TypeError, ValueError):
        return 1


def section_info(project: dict, idx: "PhotoIndex | None" = None) -> dict:
    """Разделы без собственного исполнения: {item_id: (готово подпунктов, всего подпунктов)}.

    Раздел — пункт с подпунктами, у которого статус «Не начато», нет фото и комментария.
    Такие пункты не считаются в сводке и показываются прогрессом подпунктов.
    """
    items = project.get("items") or []
    idx = idx or PhotoIndex(project)
    flags = []
    for i, it in enumerate(items):
        ex = item_extra(it)
        flags.append(has_children(items, i) and status_key(it) == "todo"
                     and not (it.get("comment") or "").strip() and not idx.for_item(it)
                     and not idx.files_for(it) and not (ex["required"] or ex["note"] or ex["links"]))
    out = {}
    for i, it in enumerate(items):
        if not flags[i]:
            continue
        done = total = 0
        for j in range(i + 1, len(items)):
            if _level(items[j]) <= _level(it):
                break
            if flags[j]:
                continue
            k = status_key(items[j])
            if k == "na":
                continue
            total += 1
            done += k in COMPLETE
        out[it.get("id") or f"#{i}"] = (done, total)
    return out


def section_key(item: dict, index_in_project: int | None = None) -> str:
    return item.get("id") or f"#{index_in_project}"


def summary(project: dict) -> dict:
    """Сводка для титула: счётчики статусов (без разделов), готовность, фото, период съёмки."""
    items = project.get("items") or []
    idx = PhotoIndex(project)
    sections = section_info(project, idx)
    counts = {k: 0 for k in STATUS_ORDER}
    for i, it in enumerate(items):
        if section_key(it, i) in sections:
            continue
        counts[status_key(it)] += 1
    total = sum(counts.values())
    in_items = sum(1 for p in idx.photos if p.get("item_id") in idx.item_ids and p.get("item_id"))
    unsorted = len(idx.photos) - in_items
    applicable = total - counts["na"]
    complete = sum(counts[k] for k in COMPLETE)
    dts = sorted(d for d in (photo_datetime(p) for p in idx.photos) if d)
    return {
        "total": total,
        "files": len(idx.files),
        "sections": len(sections),
        "counts": counts,
        "complete": complete,
        "progress": (complete / applicable) if applicable else 0.0,
        "photos": len(idx.photos),
        "photos_in_items": in_items,
        "photos_unsorted": unsorted,
        "first_photo": dts[0] if dts else None,
        "last_photo": dts[-1] if dts else None,
    }


def period_text(s: dict) -> str:
    a, b = s.get("first_photo"), s.get("last_photo")
    if not a:
        return ""
    if a.date() == b.date():
        return a.strftime("%d.%m.%Y")
    return f"{a.strftime('%d.%m.%Y')} — {b.strftime('%d.%m.%Y')}"


def only_status_text(opts: dict) -> str:
    st = opts.get("only_status")
    if not st:
        return ""
    return ", ".join(STATUS_LABELS[k] for k in STATUS_ORDER if k in st)


# ---------------------------------------------------------------------------
# Имена файлов и папок
# ---------------------------------------------------------------------------

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(s: str, limit: int = 80, fallback: str = "без названия") -> str:
    """Безопасное имя файла/папки для Windows, macOS и Linux (≤ limit символов)."""
    t = _BAD_CHARS.sub(" ", str(s or ""))
    t = re.sub(r"\s+", " ", t).strip().strip(".").strip()
    if len(t) > limit:
        cut = t[:limit]
        sp = cut.rfind(" ")
        if sp > limit * 0.6:
            cut = cut[:sp]
        t = cut.rstrip(" .,;:—–-")
    if not t or t.upper() in _RESERVED:
        t = fallback
    return t


def ensure_ext_jpg(name: str) -> str:
    stem = os.path.splitext(name)[0]
    return f"{stem}.jpg"
