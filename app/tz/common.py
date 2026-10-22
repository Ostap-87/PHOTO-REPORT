"""Общие помощники разбора ТЗ, не зависящие от формата файла.

Здесь собрано:
- очистка текста и экранирование HTML;
- распознавание маркеров нумерации («1.», «1.2», «1)», «а)», «I.», «—», «•»);
- количество и единицы измерения («12 шт.», «6×4 м», «150 м²»);
- короткое название пункта из длинного текста;
- признаки шапки, подписей и названия документа;
- сборка иерархии пунктов (класс Outline) и разбор таблиц работ.

Парсеры форматов (docx, pdf, таблицы, текст) превращают файл в поток элементов
Para / TableBlock и вызывают build_items(), который возвращает название и пункты.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Текст и HTML
# ---------------------------------------------------------------------------

_WS = re.compile(r"[ \t  -​  　﻿]+")


def clean_text(s: str | None) -> str:
    """Убирает лишние пробелы и пустые строки, сохраняет переносы строк."""
    if not s:
        return ""
    s = str(s).replace("\r\n", "\n").replace("\r", "\n").replace("­", "").replace("\x0b", "\n")
    lines = [_WS.sub(" ", ln).strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


def one_line(s: str | None) -> str:
    """Текст в одну строку с одиночными пробелами."""
    if not s:
        return ""
    return _WS.sub(" ", str(s).replace("\n", " ").replace("\r", " ").replace("­", "")).strip()


def esc(s: str | None) -> str:
    """Экранирование для HTML (текст и значения атрибутов)."""
    return _html.escape(s or "", quote=True)


def src_para(index: int) -> dict:
    """Источник пункта — абзац документа (data-src="p<index>")."""
    return {"kind": "para", "index": index, "page": None, "ref": f"p{index}"}


def src_row(table: int, row: int, page: int | None = None) -> dict:
    """Источник пункта — строка таблицы (data-src="t<table>-r<row>")."""
    return {"kind": "row", "index": row, "table": table, "row": row, "page": page,
            "ref": f"t{table}-r{row}"}


# ---------------------------------------------------------------------------
# Маркеры нумерации
# ---------------------------------------------------------------------------

@dataclass
class Marker:
    """Распознанный маркер в начале абзаца."""

    kind: str   # dotted | paren | letter | bullet | section (римские, «Раздел N»)
    num: str    # нормализованный номер: «1.2», «1)», «а)», «II», «» для маркеров-точек
    depth: int  # для dotted — число уровней в номере, иначе 1
    rest: str   # текст после маркера


_RE_DOTTED = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,5})(\.?)(?:\s+|(?=[А-ЯЁA-Z«\"(]))(?=\S)")
_RE_PAREN = re.compile(r"^\(?(\d{1,3})\)\s*(?=\S)")
_RE_LETTER = re.compile(r"^\(?([а-яёa-z])\)\s*(?=\S)")
_RE_ROMAN = re.compile(r"^(?=[IVXLC])(C{0,3}(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3}))\.\s*(?=\S)")
_RE_BULLET = re.compile(r"^(?:[–—•·▪■□●○◦►▸➢➤✓✔❖]\s*|[-*]\s+)(?=\S)")
_RE_SECTION = re.compile(r"^(?:раздел|глава|часть)\s+(\d{1,3}|[IVXLC]{1,6})\b\.?\s*[.:—–-]?\s*", re.I)
_UNIT_AFTER_NUM = re.compile(r"^(?:м|мм|см|км|шт|кв|куб|кг|л|т|г|руб|%|°)\b", re.I)


def classify_marker(text: str) -> Marker | None:
    """Определяет маркер нумерации в начале строки; None — маркера нет."""
    t = text.lstrip()
    if not t:
        return None
    m = _RE_SECTION.match(t)
    if m:
        return Marker("section", m.group(1), 1, t[m.end():].strip() or t)
    m = _RE_DOTTED.match(t)
    if m:
        num, dot = m.group(1), m.group(2)
        rest = t[m.end():].strip()
        if (dot or "." in num) and rest and not (not dot and _UNIT_AFTER_NUM.match(rest)):
            return Marker("dotted", num, num.count(".") + 1, rest)
    m = _RE_PAREN.match(t)
    if m:
        return Marker("paren", m.group(1) + ")", 1, t[m.end():].strip())
    m = _RE_LETTER.match(t)
    if m:
        return Marker("letter", m.group(1) + ")", 1, t[m.end():].strip())
    m = _RE_ROMAN.match(t)
    if m and m.group(1):
        return Marker("section", m.group(1), 1, t[m.end():].strip())
    m = _RE_BULLET.match(t)
    if m:
        return Marker("bullet", "", 1, t[m.end():].strip())
    return None


def normalize_num(s: str) -> str:
    """«1.2.» → «1.2», «1 .» → «1»; прочие номера без изменений."""
    s = one_line(s).strip()
    s2 = s.rstrip(".").strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){0,5}", s2):
        return s2
    return s


# ---------------------------------------------------------------------------
# Количество и единицы измерения
# ---------------------------------------------------------------------------

# (шаблон, нормализованная единица, категория). Категория определяет приоритет
# при выборе количества из текста: count > area > volume > run > dim > length > mass.
_UNITS: list[tuple[str, str, str]] = [
    (r"кв\.?\s*м(?:етр(?:ов|а)?)?\.?|м2|м²|m2|m²", "м²", "area"),
    (r"куб\.?\s*м(?:етр(?:ов|а)?)?\.?|м3|м³|m3|m³", "м³", "volume"),
    (r"п\.\s*м\.?|пог\.?\s*м(?:етр(?:ов|а)?)?\.?|м\.\s*п\.?|пм\b", "п.м", "run"),
    (r"шт(?:ук[аи]?|\.)?", "шт", "count"),
    (r"компл(?:ект(?:ов|а)?|\.)?|кмпл\.?|к-т", "компл", "count"),
    (r"ед(?:иниц[аы]?|\.)?(?:\s*изм\.?)?", "ед", "count"),
    (r"чел(?:овек[а]?|\.)?", "чел", "count"),
    (r"перс(?:он[аы]?|\.)?", "перс", "count"),
    (r"усл(?:уг[аи]?|\.)?", "усл", "count"),
    (r"мест[ао]?", "мест", "count"),
    (r"порц(?:ий|ии|ия|\.)?", "порц", "count"),
    (r"пар[аы]?", "пар", "count"),
    (r"рул(?:он(?:ов|а)?|\.)?", "рул", "count"),
    (r"лист(?:ов|а)?", "лист", "count"),
    (r"уп(?:аковк[аи]|ак\.?|\.)", "уп", "count"),
    (r"лини[йяи]|лин\.", "линия", "count"),
    (r"точ(?:ек|ки|ка|\.)", "точка", "count"),
    (r"км", "км", "length"),
    (r"м(?:етр(?:ов|а)?)?\.?", "м", "length"),
    (r"кг", "кг", "mass"),
    (r"л\.?", "л", "mass"),
    # Только для колонки «Ед. изм.», из текста не берём (обычно это сроки, а не объём):
    (r"час(?:ов|а)?|ч\.", "ч", "time"),
    (r"смен[аы]?", "смена", "time"),
    (r"дн(?:ей|я|\.)?|день", "дн", "time"),
    (r"сут(?:ок|ки|\.)?", "сут", "time"),
    (r"мес(?:яц(?:ев|а)?|\.)?", "мес", "time"),
    (r"мм", "мм", "param"),
    (r"см", "см", "param"),
]
_UNIT_ALT = "|".join(f"(?:{p})" for p, _, _ in _UNITS)
_UNIT_RE = [(re.compile(rf"^(?:{p})$", re.I), norm, cat) for p, norm, cat in _UNITS]
_NUM = r"\d{1,3}(?:[  ]\d{3})+|\d+(?:[.,]\d+)?"
_NUM_SIMPLE = r"\d+(?:[.,]\d+)?"
_QTY_RE = re.compile(
    rf"(?<![\d.,×xх*/])(?P<a>{_NUM_SIMPLE})\s*[×xх*]\s*(?P<b>{_NUM_SIMPLE})(?:\s*[×xх*]\s*(?P<c>{_NUM_SIMPLE}))?"
    rf"\s*(?P<du>{_UNIT_ALT})(?![а-яёa-z0-9²³])"
    rf"|(?<![\d.,×xх*/])(?P<n>{_NUM})\s*(?P<u>{_UNIT_ALT})(?![а-яёa-z0-9²³])",
    re.I,
)
_PARAM_BEFORE = re.compile(
    r"(высот\w*|ширин\w*|длин\w*|глубин\w*|толщин\w*|диаметр\w*|радиус\w*|сечени\w*|"
    r"расстояни\w*|отступ\w*|зазор\w*|шаг\w*|\bd|ø|⌀)\s*[:=]?\s*(?:не\s+(?:менее|более)|до|от|около)?\s*$",
    re.I,
)
# Ограничения («не менее 1,5 м», «до 50 м») для длины/массы — это параметр, а не объём работ.
_BOUND_BEFORE = re.compile(r"(не\s+(?:менее|более|ниже|выше)|\bдо|\bот|около|свыше|запас\w*|\bпо)\s*$", re.I)
_CAT_PRIORITY = {"count": 0, "area": 1, "volume": 2, "run": 3, "dim": 4, "length": 5, "mass": 6}


def norm_unit(s: str | None) -> str:
    """Нормализует единицу измерения: «шт.» → «шт», «кв. м» → «м²»; неизвестные — как есть."""
    u = one_line(s)
    if not u:
        return ""
    for rx, norm, _cat in _UNIT_RE:
        if rx.match(u):
            return norm
    return u.rstrip(".").strip()


def _to_number(s: str):
    s = s.replace(" ", " ").replace(" ", "")
    if re.fullmatch(r"\d+", s):
        return int(s)
    try:
        v = float(s.replace(",", "."))
    except ValueError:
        return None
    return int(v) if v.is_integer() else v


def parse_qty(value) -> tuple[object, str]:
    """Количество из ячейки «Кол-во»: число, «12 шт», «1 200,5»; нечисловое («по факту») — строкой."""
    if value is None:
        return None, ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return (int(v) if v.is_integer() else v), ""
    s = one_line(str(value))
    if not s:
        return None, ""
    m = re.match(rf"^(?P<n>{_NUM})\s*(?P<u>.*)$", s)
    if m:
        n = _to_number(m.group("n"))
        if n is not None:
            return n, norm_unit(m.group("u")) if m.group("u") else ""
    return s, ""


def extract_qty(text: str) -> tuple[object, str]:
    """Ищет количество в тексте пункта («12 шт.», «6×4 м», «150 м²»).

    Параметры (высота, диаметр, «в течение 3 часов») не считаются количеством.
    При нескольких кандидатах берётся самый «счётный» (шт > м² > м³ > п.м > a×b > м),
    среди равных — последний (количество обычно пишут в конце пункта).
    """
    if not text:
        return None, ""
    best = None
    for m in _QTY_RE.finditer(text):
        before = text[max(0, m.start() - 30):m.start()]
        if _PARAM_BEFORE.search(before):
            continue
        if m.group("n"):
            unit = norm_unit(m.group("u"))
            cat = next((c for rx, n, c in _UNIT_RE if n == unit), "other")
            if cat not in _CAT_PRIORITY:
                continue
            value = _to_number(m.group("n"))
        else:
            unit = norm_unit(m.group("du"))
            cat = next((c for rx, n, c in _UNIT_RE if n == unit), "other")
            if cat not in ("length", "area", "run"):
                continue
            parts = [m.group("a"), m.group("b")] + ([m.group("c")] if m.group("c") else [])
            value = "×".join(p.replace(".", ",") for p in parts)
            cat = "dim"
        if value is None:
            continue
        if cat in ("length", "run", "dim", "mass") and _BOUND_BEFORE.search(before):
            continue
        pr = _CAT_PRIORITY[cat]
        if best is None or pr <= best[0]:
            best = (pr, value, unit)
    if best is None:
        return None, ""
    return best[1], best[2]


# ---------------------------------------------------------------------------
# Название пункта
# ---------------------------------------------------------------------------

# Сокращения, после точки которых предложение не заканчивается («г. Москва», «ул. Мира»).
_NO_SPLIT_ABBR = {
    "г", "гг", "ул", "д", "им", "пр", "просп", "пер", "обл", "р", "с", "п", "пп", "т", "ст", "см",
    "рис", "табл", "стр", "тел", "гр", "млн", "тыс", "млрд", "руб", "коп", "кв", "корп", "оф",
    "пом", "эт", "мкр", "наб", "пл", "ш", "тер", "св", "т.е", "т.д", "т.п", "т.ч", "напр", "др",
    "прим", "мин", "макс", "ок", "вкл", "искл", "арт", "no", "n", "кат", "мод", "инв", "зав",
}


def first_sentence(text: str) -> str:
    """Первое предложение текста (с учётом сокращений и инициалов)."""
    for m in re.finditer(r"[.!?;](?=\s+[А-ЯЁA-Z«\"(])|[!?;](?=\s)", text):
        end = m.start()
        if text[end] == ".":
            w = re.search(r"([А-Яа-яЁёA-Za-z.]{1,8})$", text[:end])
            if w:
                word = w.group(1).strip(".")
                if word.lower() in _NO_SPLIT_ABBR or (len(word) == 1 and word.isupper()):
                    continue
        return text[:end + 1]
    return text


def make_title(text: str, limit: int = 120) -> str:
    """Короткое название пункта: первое предложение, не длиннее limit символов, с заглавной буквы."""
    title = _make_title(text, limit)
    if title[:1].islower():
        title = title[:1].upper() + title[1:]
    return title


def _make_title(text: str, limit: int) -> str:
    t = one_line(text)
    if not t:
        return ""
    s = first_sentence(t).strip().rstrip(" ;,.:")
    if len(s) <= limit:
        if len(s) > 90 and ":" in s:
            head = s.split(":", 1)[0].strip()
            if len(head) >= 8:
                return head
        return s
    for sep in (":", " — ", " – ", " - ", "; ", ", ", " ("):
        idx = s.find(sep)
        if 15 <= idx <= limit:
            return s[:idx].strip()
    cut = s[:limit]
    sp = cut.rfind(" ")
    if sp > limit // 2:
        cut = cut[:sp]
    return cut.rstrip(" ,;:—–-") + "…"


# ---------------------------------------------------------------------------
# Шапка, подписи, название документа
# ---------------------------------------------------------------------------

_TITLE_RE = re.compile(r"техническ\w*\s+задани\w*|^т\.?\s?з\.?(?:\s|$)|^задание\s+на\b|^описание\s+объекта\s+закупки", re.I)
_TITLE_CONT_RE = re.compile(r"^(на|по|для|о|об|к|в|на\s+выполнение|на\s+оказание)\s", re.I)
_HEADER_RE = re.compile(
    r"^(утверждаю|утвержден[оа]?|согласовано|согласован[оа]?|кому\b|от\s+кого|от\s*:|исх\.|вх\.|"
    r"приложение\s*(№|n|к\b|\d)|к\s+договору|к\s+контракту|к\s+государственному|к\s+муниципальному|"
    r"инн\b|кпп\b|огрн|окпо|р/с|р/сч|к/с|бик\b|банк\b|адрес\b|юр\.\s*адрес|тел\.|телефон|e-?mail|эл\.\s*почта|"
    r"(заказчик|исполнитель|подрядчик|поставщик|покупатель|продавец)\s*:|"
    r"генеральн\w+\s+директор|директор\b|руководител\w*|начальник\w*|главн\w+\s+инженер|"
    r"г\.\s*[А-ЯЁ]|дата\s*:|м\.\s?п\.|"
    r"(общество\s+с\s+ограниченной|ооо|оао|зао|пао|ао|ип|фгуп|гуп|муп|гбу|мбу|фгбу|"
    r"государственн\w+|муниципальн\w+|федеральн\w+|министерств\w*|департамент\w*|администраци\w*)\b)",
    re.I,
)
_SIGN_RE = re.compile(
    r"_{3,}|м\.\s?п\.|^подпис[ьи]|«\s*_*\s*»\s*_+|"
    r"/\s*[А-ЯЁ]\.\s?[А-ЯЁ]\.\s*[А-ЯЁ][а-яё]+\s*/?\s*$|"
    r"^(заказчик|исполнитель|подрядчик|поставщик|покупатель|продавец)\s*:|"
    r"^(заказчик|исполнитель|подрядчик|поставщик)\s*$",
    re.I,
)
_STOP_RE = re.compile(
    r"^(подписи\s+сторон|реквизиты(\s+и\s+подписи)?(\s+сторон)?|адреса\s+и\s+реквизиты|"
    r"юридические\s+адреса|подписи)\s*[.:]?\s*$",
    re.I,
)
_STOP_APPENDIX_RE = re.compile(r"^приложени[еяй]\s*(№|n|\d|:|$)", re.I)
_LABEL_RE = re.compile(r"^(примечани|внимание|важно|комментари|справочно)", re.I)


def is_title_line(text: str) -> bool:
    return bool(_TITLE_RE.search(one_line(text)))


def is_header_like(text: str) -> bool:
    return bool(_HEADER_RE.search(one_line(text)))


def is_signature_like(text: str) -> bool:
    return bool(_SIGN_RE.search(one_line(text)))


def is_stop_heading(text: str) -> bool:
    return bool(_STOP_RE.match(one_line(text)))


def pretty_title(text: str) -> str:
    """«ТЕХНИЧЕСКОЕ ЗАДАНИЕ на …» → «Техническое задание на …»."""
    t = one_line(text).strip(" .")
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        t = t.lower()
        t = t[:1].upper() + t[1:]
    else:
        t = re.sub(r"^(ТЕХНИЧЕСКОЕ\s+ЗАДАНИЕ)", "Техническое задание", t)
    return t[:200]


def looks_like_caps_heading(text: str) -> bool:
    t = one_line(text)
    letters = [c for c in t if c.isalpha()]
    return (len(letters) >= 3 and len(t) <= 100 and all(c.isupper() for c in letters)
            and not t.endswith("."))


# ---------------------------------------------------------------------------
# Поток элементов документа
# ---------------------------------------------------------------------------

@dataclass
class Para:
    """Абзац документа (или строка текста / строка PDF после склейки)."""

    text: str
    src: dict
    bold: bool = False
    heading: int = 0            # ранг заголовка по стилю (1 — Heading 1); 0 — не заголовок
    is_title: bool = False      # стиль «Title»
    centered: bool = False
    auto: tuple | None = None   # автонумерация Word: (номер, ilvl, маркер-точка?)
    indent: int = 0             # уровень отступа (вложенные списки txt/md)
    sub_list: int = 0           # «List Number 2» → 1: вложенный список по стилю


@dataclass
class TableBlock:
    """Таблица документа: строки ячеек и источник каждой строки."""

    rows: list[list[str]]
    srcs: list[dict]
    merged: list[bool] = field(default_factory=list)  # строка — одна объединённая ячейка
    bold: list[bool] = field(default_factory=list)
    name: str = ""                                    # имя листа Excel


# ---------------------------------------------------------------------------
# Иерархия пунктов
# ---------------------------------------------------------------------------

class Outline:
    """Собирает пункты и вычисляет уровни.

    Правила уровней:
    - ненумерованный заголовок (стиль Heading, жирный, «Раздел N», «I.») — уровень по рангу (1);
    - нумерованный пункт «1.2.3» — уровень заголовка + глубина номера, причём глубина
      отсчитывается от первого номера после заголовка (если под заголовком сразу «1.1», это уровень 2);
    - «1)», «а)», «—» — относительные маркеры: на уровень глубже последнего нумерованного
      пункта; новый тип маркера уходит ещё глубже, уже встречавшийся — возвращается на свой уровень;
    - абзац без маркера после пункта дописывается в его текст; два и больше коротких абзацев
      под заголовком становятся подпунктами;
    - в таблице строки-разделы — на уровень ниже текущего заголовка документа.
    """

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.head = 0          # уровень последнего заголовка документа
        self.base = 0          # база для нумерованных пунктов
        self.anchor = 0        # уровень последнего «якорного» пункта
        self.d0: int | None = None  # глубина первого номера после заголовка
        self.rel: list[str] = []
        self.pending: list[tuple[str, dict]] = []
        self.loose: list[tuple[str, dict]] = []   # абзацы вне пунктов (для запасного режима)
        self.stopped = False
        self._saved: tuple | None = None
        self.in_table = False

    # -- служебное ---------------------------------------------------------
    def _add(self, num: str, level: int, text: str, src: dict, qty=None, unit: str = "",
             heading: bool = False) -> dict:
        it = {"num": num, "level": max(1, int(level)), "_paras": text.split("\n"), "qty": qty,
              "unit": unit or "", "source": src, "_heading": heading}
        self.items.append(it)
        return it

    def _flush(self) -> None:
        if not self.pending:
            return
        parent = self.items[-1] if self.items else None
        many = len(self.pending) >= 2 and all(len(t) <= 300 for t, _ in self.pending)
        if parent is not None and parent["_heading"] and many:
            lvl = parent["level"] + 1
            for t, s in self.pending:
                self._add("", lvl, t, s)
        elif parent is not None:
            parent["_paras"].extend(t for t, _ in self.pending)
        self.pending = []

    # -- элементы ----------------------------------------------------------
    def heading(self, text: str, rank: int, src: dict, num: str = "") -> None:
        self._flush()
        if self.in_table:
            level = self.head + 1
        else:
            level = max(1, rank)
            self.head = level
        self.base = level
        self.anchor = level
        self.d0 = None
        self.rel = []
        self._add(num, level, text, src, heading=True)

    def numbered(self, num: str, depth: int, text: str, src: dict, qty=None, unit: str = "") -> None:
        self._flush()
        if self.d0 is None:
            self.d0 = depth
        level = self.base + 1 + max(0, depth - self.d0)
        self.anchor = level
        self.rel = []
        self._add(num, level, text, src, qty, unit)

    def relative(self, mtype: str, num: str, text: str, src: dict, qty=None, unit: str = "") -> None:
        self._flush()
        if mtype in self.rel:
            self.rel = self.rel[: self.rel.index(mtype) + 1]
        else:
            self.rel.append(mtype)
        level = self.anchor + self.rel.index(mtype) + 1
        self._add(num, level, text, src, qty, unit)

    def plain(self, text: str, src: dict) -> None:
        if not self.items or is_signature_like(text):
            self.loose.append((text, src))
            return
        last = self.items[-1]
        if last["_heading"]:
            self.pending.append((text, src))
        else:
            last["_paras"].append(text)

    def table_begin(self) -> None:
        self._flush()
        self._saved = (self.base, self.anchor, self.d0, list(self.rel))
        self.in_table = True
        self.base = self.head
        self.anchor = self.head
        self.d0 = None
        self.rel = []

    def table_row(self, text: str, src: dict, qty=None, unit: str = "") -> None:
        """Строка таблицы работ без номера — подпункт текущего раздела таблицы."""
        self._flush()
        self._add("", self.base + 1, text, src, qty, unit)

    def table_end(self) -> None:
        self._flush()
        self.in_table = False
        if self._saved:
            self.base, self.anchor, self.d0, self.rel = self._saved
        self._saved = None

    # -- итог ----------------------------------------------------------------
    def finish(self) -> list[dict]:
        self._flush()
        items = self.items
        if not items:
            # Запасной режим: маркеров и заголовков нет — каждый абзац становится пунктом.
            for t, s in self.loose:
                if is_signature_like(t) or is_header_like(t) or is_title_line(t):
                    continue
                items.append({"num": "", "level": 1, "_paras": [t], "qty": None, "unit": "",
                              "source": s, "_heading": False})
        out = []
        for it in items:
            paras = [one_line(p) for p in it["_paras"] if one_line(p)]
            if not paras:
                continue
            text = "\n".join(paras)
            qty, unit = it["qty"], it["unit"]
            if qty is None and not it["_heading"]:
                qty, unit2 = extract_qty(paras[0])
                if qty is None and len(paras) > 1:
                    qty, unit2 = extract_qty(text)
                unit = unit or unit2
            out.append({
                "num": it["num"],
                "level": it["level"],
                "title": make_title(paras[0]),
                "text": text,
                "qty": qty,
                "unit": unit or "",
                "source": it["source"],
            })
        return out


# ---------------------------------------------------------------------------
# Таблицы работ
# ---------------------------------------------------------------------------

@dataclass
class TableMap:
    header_row: int
    name: int
    num: int | None = None
    qty: int | None = None
    unit: int | None = None
    notes: list[int] = field(default_factory=list)


def _cell_role(cell: str) -> str | None:
    c = one_line(cell).lower().replace("ё", "е")
    if not c or len(c) > 70:
        return None
    if (re.match(r"^(№|n|no\.?|#)\s*(п/?п|пп|позиции|пункта|строки)?\.?$", c)
            or re.match(r"^п/?п$", c) or c in ("номер", "поз.", "поз", "позиция", "пункт", "номер пункта")):
        return "num"
    if re.match(r"^ед(\.|\s|$)", c) or "ед.изм" in c or "ед. изм" in c or c.startswith("единиц"):
        return "unit"
    if re.match(r"^(кол-?во|кол\.|количеств|объем|qty)", c):
        return "qty"
    if re.search(r"наименовани|наим\.|вид(ы)?\s+работ|перечень|состав\s+работ|содержание\s+(работ|услуг)|"
                 r"^работ[аы]?\b|^услуг[аи]?\b|описание\s+(работ|услуг)|^мероприяти|^товар|^позиция\s+тз", c):
        return "name"
    if re.search(r"^описани|^требовани|характеристик|примечани|комментари|параметр|^срок|периодичн|"
                 r"^технические\s+требования|^содержание", c):
        return "note"
    return None


def find_table_map(rows: list[list[str]], scan: int = 40) -> TableMap | None:
    """Находит строку заголовков таблицы работ и роли колонок по синонимам.

    Если заголовков нет, но первая колонка — номера, а вторая — текст, таблица
    считается списком работ «№ | Наименование».
    """
    for r_i, row in enumerate(rows[:scan]):
        roles: dict[str, int] = {}
        notes: list[int] = []
        names: list[int] = []
        for c_i, cell in enumerate(row):
            role = _cell_role(cell)
            if role == "name":
                names.append(c_i)
            elif role == "note":
                notes.append(c_i)
            elif role and role not in roles:
                roles[role] = c_i
        if not names and notes and roles:
            names.append(notes.pop(0))
        if not names:
            continue
        if not roles and len(names) < 2:
            # Одна колонка «Наименование» без других ролей — только если строк ниже достаточно.
            nonempty = [c for c in row if one_line(c)]
            if len(nonempty) < 2:
                continue
        notes = sorted(set(notes + names[1:]))
        return TableMap(r_i, names[0], roles.get("num"), roles.get("qty"), roles.get("unit"), notes)
    # Таблица без заголовков: «1 | текст».
    body = [r for r in rows if any(one_line(c) for c in r)]
    if len(body) >= 2 and all(len(r) >= 2 for r in body):
        numbered = sum(1 for r in body if re.fullmatch(r"\d{1,3}(\.\d{1,3})*\.?", one_line(r[0]))
                       and len(one_line(r[1])) > 3)
        if numbered >= max(2, int(len(body) * 0.6)):
            return TableMap(-1, 1, 0, None, None, [])
    return None


def _is_column_numbers_row(row: list[str]) -> bool:
    vals = [one_line(c) for c in row if one_line(c)]
    return len(vals) >= 2 and all(v.isdigit() for v in vals) and [int(v) for v in vals] == list(range(1, len(vals) + 1))


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return clean_text(row[idx])


def process_table(outline: Outline, tb: TableBlock) -> bool:
    """Добавляет пункты из таблицы работ. False — таблица не похожа на перечень работ."""
    tmap = find_table_map(tb.rows)
    if tmap is None:
        return False
    outline.table_begin()
    start = tmap.header_row + 1
    for r_i in range(start, len(tb.rows)):
        row = tb.rows[r_i]
        src = tb.srcs[r_i] if r_i < len(tb.srcs) else src_row(0, r_i)
        vals = [clean_text(c) for c in row]
        nonempty = [v for v in vals if v]
        if not nonempty:
            continue
        if r_i == start and _is_column_numbers_row(row):
            continue
        first = one_line(nonempty[0]).lower()
        if re.match(r"^(итого|всего|в\s+том\s+числе|ндс)\b", first):
            continue
        name = _cell(row, tmap.name)
        num_raw = _cell(row, tmap.num)
        qty_raw = _cell(row, tmap.qty)
        unit_raw = _cell(row, tmap.unit)
        merged = tb.merged[r_i] if r_i < len(tb.merged) else False
        distinct = list(dict.fromkeys(nonempty))
        # Строка-раздел: одна объединённая ячейка или единственный текст без номера и количества.
        if merged or (len(distinct) == 1 and not num_raw and not qty_raw):
            text = one_line(distinct[0])
            if is_signature_like(text):
                continue
            mk = classify_marker(text)
            if mk and mk.kind == "dotted":
                outline.numbered(mk.num, mk.depth, mk.rest, src)
            elif mk and mk.kind == "section":
                outline.heading(mk.rest, 1, src, num=mk.num)
            else:
                outline.heading(text, 1, src)
            continue
        if not name:
            continue
        notes = [_cell(row, c) for c in tmap.notes if _cell(row, c)]
        text = name + ("\n" + "\n".join(notes) if notes else "")
        qty, unit_in_qty = parse_qty(qty_raw) if qty_raw else (None, "")
        unit = norm_unit(unit_raw) if unit_raw else unit_in_qty
        num = normalize_num(num_raw)
        if num and re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){0,5}", num):
            outline.numbered(num, num.count(".") + 1, text, src, qty, unit)
            continue
        mk = classify_marker(name) if not num else None
        if mk and mk.kind == "dotted":
            outline.numbered(mk.num, mk.depth, mk.rest + ("\n" + "\n".join(notes) if notes else ""), src, qty, unit)
        elif num:
            outline.relative("tbl", num, text, src, qty, unit)
        else:
            outline.table_row(text, src, qty, unit)
    outline.table_end()
    return True


# ---------------------------------------------------------------------------
# Сборка пунктов из потока
# ---------------------------------------------------------------------------

def _find_title(stream: list) -> tuple[str, int]:
    """Название документа и индекс последнего элемента шапки (-1 — шапки нет)."""
    limit = min(len(stream), 60)
    for i in range(limit):
        el = stream[i]
        if not isinstance(el, Para):
            continue
        mk = classify_marker(el.text)
        if (el.auto or (mk is not None and mk.kind != "bullet")) and not is_title_line(el.text):
            break  # название ищем только до первого нумерованного пункта
        if is_title_line(el.text) and len(el.text) <= 250:
            title = el.text
            end = i
            for j in range(i + 1, min(i + 3, len(stream))):
                nxt = stream[j]
                if not isinstance(nxt, Para) or nxt.auto or classify_marker(nxt.text):
                    break
                t = one_line(nxt.text)
                if (_TITLE_CONT_RE.match(t) or (nxt.centered and len(t) <= 200 and not is_header_like(t))) \
                        and not t.endswith(":") and len(t) <= 250:
                    title = f"{title} {t}"
                    end = j
                else:
                    break
            return pretty_title(title), end
    for i in range(limit):
        el = stream[i]
        if isinstance(el, Para) and el.is_title:
            return pretty_title(el.text), i
    return "", -1


def _is_heading_like(p: Para) -> bool:
    t = one_line(p.text)
    if not t or _LABEL_RE.match(t) or len(t) > 150:
        return False
    if p.heading > 0:
        return True
    if t.endswith(".") or t.endswith(";") or t.endswith(","):
        return False
    return p.bold or looks_like_caps_heading(t)


def build_items(stream: list, fallback_title: str = "") -> tuple[str, list[dict]]:
    """Превращает поток Para/TableBlock в название документа и список пунктов."""
    title, title_end = _find_title(stream)
    outline = Outline()
    ranks = sorted({el.heading for i, el in enumerate(stream)
                    if i > title_end and isinstance(el, Para) and el.heading > 0})
    rank_map = {r: i + 1 for i, r in enumerate(ranks)}

    for idx, el in enumerate(stream):
        if idx <= title_end:
            continue
        if outline.stopped:
            break
        if isinstance(el, TableBlock):
            process_table(outline, el)
            continue
        text = clean_text(el.text)
        if not text:
            continue
        flat = one_line(text)
        started = bool(outline.items)

        if el.auto is not None:
            num, ilvl, bullet = el.auto
            if is_stop_heading(flat) and started:
                outline.stopped = True
                continue
            if bullet:
                outline.relative(f"bullet{ilvl}", "", text, el.src)
                continue
            if num and re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){0,5}\.?", num.strip()):
                outline.numbered(normalize_num(num), ilvl + 1 + el.sub_list, text, el.src)
                continue
            mk = classify_marker(f"{num} {flat}") if num else None
            if mk is None:
                if num:
                    outline.relative(f"auto{ilvl}", normalize_num(num), text, el.src)
                else:
                    outline.relative(f"auto{ilvl}", "", text, el.src)
            elif mk.kind == "dotted":
                outline.numbered(normalize_num(num), ilvl + 1 + el.sub_list, text, el.src)
            elif mk.kind == "section":
                outline.heading(text, 1, el.src, num=mk.num)
            else:
                outline.relative(f"{mk.kind}{ilvl}", mk.num, text, el.src)
            continue

        mk = classify_marker(flat)
        if mk is not None and mk.kind != "bullet" and mk.rest:
            if is_stop_heading(mk.rest) and started:
                outline.stopped = True
                continue
            if mk.kind == "dotted":
                if el.indent and mk.depth == 1:
                    outline.relative(f"num-i{el.indent}", mk.num, mk.rest, el.src)
                else:
                    outline.numbered(mk.num, mk.depth + el.sub_list, mk.rest, el.src)
            elif mk.kind == "section":
                outline.heading(mk.rest, 1, el.src, num=mk.num)
            else:
                outline.relative(f"{mk.kind}-i{el.indent}", mk.num, mk.rest, el.src)
            continue
        if mk is not None and mk.kind == "bullet" and mk.rest:
            if is_signature_like(mk.rest):
                continue
            outline.relative(f"bullet-i{el.indent}", "", mk.rest, el.src)
            continue

        if _is_heading_like(el):
            if is_stop_heading(flat) or (started and _STOP_APPENDIX_RE.match(flat)):
                if started:
                    outline.stopped = True
                continue
            if is_signature_like(flat) or (not started and (is_header_like(flat) or is_title_line(flat))):
                outline.loose.append((text, el.src))
                continue
            rank = rank_map.get(el.heading, 1) if el.heading else 1
            outline.heading(text, rank, el.src)
            continue

        if started and _STOP_APPENDIX_RE.match(flat):
            outline.stopped = True
            continue
        outline.plain(text, el.src)

    items = outline.finish()
    return (title or fallback_title), items


def title_from_filename(filename: str) -> str:
    import os
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    stem = re.sub(r"[_]+", " ", stem).strip()
    return stem or "Техническое задание"


def decode_text(data: bytes) -> str:
    """Текст из байтов: UTF-8 (с BOM и без), UTF-16 по BOM, иначе Windows-1251."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1251", "replace")


def table_html(rows: list[list[str]], ti: int, spans: dict | None = None, caption: str = "") -> str:
    """HTML-таблица с data-src="t<ti>-r<row>" на каждой строке.

    spans: {(row, col): (rowspan, colspan)} для объединённых ячеек; ячейки, накрытые
    объединением, пропускаются.
    """
    spans = spans or {}
    covered = set()
    for (r, c), (rs, cs) in spans.items():
        for dr in range(rs):
            for dc in range(cs):
                if dr or dc:
                    covered.add((r + dr, c + dc))
    out = []
    if caption:
        out.append(f'<h3 class="tz-h">{esc(caption)}</h3>')
    out.append(f'<table class="tz-table" data-table="{ti}"><tbody>')
    for r, row in enumerate(rows):
        tds = []
        for c, val in enumerate(row):
            if (r, c) in covered:
                continue
            attrs = ""
            if (r, c) in spans:
                rs, cs = spans[(r, c)]
                if rs > 1:
                    attrs += f' rowspan="{rs}"'
                if cs > 1:
                    attrs += f' colspan="{cs}"'
            tds.append(f"<td{attrs}>{esc(clean_text(val)).replace(chr(10), '<br>')}</td>")
        out.append(f'<tr data-src="t{ti}-r{r}">{"".join(tds)}</tr>')
    out.append("</tbody></table>")
    return "".join(out)
