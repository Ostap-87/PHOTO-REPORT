from __future__ import annotations
# -*- coding: utf-8 -*-
"""«Реестр подтверждений» без ИИ: правила по тексту пунктов ТЗ, работает без интернета и ключа.

Строка реестра появляется, если пункт:
* лежит в разделе об отчётности или сдаче («Требования к отчёту», «Отчётность», «Приёмка»), или
* требует передать результат: глагол передачи («предоставляет», «передаёт», «сдаёт», «направляет»…)
  и в том же предложении — что именно (фото, скриншоты, видео, отчёт, список, резюме, макеты, ссылки…), или
* прямо называет фото- и видеоматериалы (фотографии, фотосъёмка, видеозапись, видеоролик, скриншоты), или
* (по флажку with_equipment) перечисляет оборудование и оснащение с количествами — «по умолчанию фото».
«Что приложить» собирается из найденного: вид материала, количество («не менее 500 фотографий»), форма
(в бумажном виде, скан-копия, электронный носитель, формат Excel…). «Требуют уточнения» — пункты с
формулировками «по согласованию», «при необходимости», «иные варианты» и т. п.
Результат — тот же словарь, что у ИИ (rows, clarify, summary): build.apply() одинаково переносит его в проект.
"""
import re

# Вид материала: (ключ, регулярка, подпись в «Что приложить»)
KINDS = [
    ("photo", r"фотографи\w*|фотосъ[её]мк\w*|фотоотч[её]т\w*|фотоматериал\w*|\bфото\b(?!\s*-?\s*зон)(?!\s*-\s*и)",
     "Фото"),
    ("screen", r"скриншот\w*|снимк\w*\s+экрана", "Скриншоты"),
    ("video", r"видеозапис\w*|видеоролик\w*|видеоматериал\w*|запис\w*\s+трансляци\w*|фото-?\s*и\s*-?\s*видео\w*",
     "Видео"),
    ("links", r"ссылк\w*|публикаци\w*|размещени\w*\s+(?:в|на)\s+(?:сайт|соц|сми|канал|интернет)\w*", "Ссылки на размещения"),
    ("doc", r"отч[её]т\w*|спис(?:ок|ка|ки)\b|реестр\w*|резюме|дизайн-?\s*макет\w*|макет\w*|протокол\w*|\bакт(?:ы|а|ов)?\b|"
            r"справк\w*|сертификат\w*|документ\w*|презентаци\w*|анкет\w*", "Документ"),
]
# только глаголы (не причастия вроде «предоставляемых»)
DELIVER = re.compile(r"\b(?:предоставля(?:ет|ют)|предостав(?:ить|ит|ят)|переда(?:ет|ёт|ют|ть|ются)|сда(?:ет|ёт|ют|ть)|"
                     r"направля(?:ет|ют)|направ(?:ить|ит)|представля(?:ет|ют)|представ(?:ить|ит)|выда(?:ет|ёт|ют)|"
                     r"обеспечивает\s+создание|формирует|подготавливает|разрабатывает)\b", re.I)
REF = re.compile(r"пункт\w*\s+((?:\d+(?:\.\d+)+\.?(?:\s*(?:,|и|–|-)\s*)?)+)", re.I)
STRONG = re.compile(r"фотографи\w*|фотосъ[её]мк\w*|фотоотч[её]т\w*|видеозапис\w*|видеоролик\w*|скриншот\w*", re.I)
REPORT_SECTION = re.compile(r"отч[её]т|отч[её]тн|сдач|при[её]мк", re.I)
EQUIPMENT = re.compile(r"оснащени\w*|оборудовани\w*|оформлени\w*|застройк\w*|брендирован\w*|монтаж\w*|стенд\w*", re.I)
QTY = re.compile(r"\d+\s*(?:\([^)]{1,40}\)\s*)?(?:шт\b|штук\w*|комплект\w*|посадочн\w*|единиц\w*|ед\.)", re.I)

COUNT_WORD = r"(фотографи\w*|фото\w*|скриншот\w*|видеоролик\w*|видеозапис\w*)"
COUNTS = [
    re.compile(r"(не\s+менее|не\s+более|в\s+количестве)\s+(\d[\d\s]*)\s*(?:\([^)]{1,40}\)\s*)?(?:штук\w*\s*)?" + COUNT_WORD,
               re.I),
    re.compile(COUNT_WORD + r"\s+в\s+количестве\s+(\d[\d\s]*)", re.I),
]
FORMS = [
    (r"в\s+бумажном\s+виде", "в бумажном виде"),
    (r"скан-?\s*копи\w*", "скан-копия"),
    (r"в\s+электронном\s+виде", "в электронном виде"),
    (r"электронн\w*\s+носител\w*", "на электронном носителе"),
    (r"в\s+формате\s+(excel|xlsx?|pdf|word|docx?|jpe?g|png|mp4|mov)", "формат {0}"),
    (r"заверенн\w*\s+подпись\w*", "с подписью и печатью"),
]
VAGUE = [
    (r"по\s+согласованию\s+с\s+заказчиком", "«по согласованию с Заказчиком»: не указано, чем и в каком виде подтверждать"),
    (r"(?:при|в\s+случае)\s+необходимости", "«при необходимости»: не ясно, обязателен ли материал"),
    (r"ины[ехм]\s+(?:вариант|материал|документ)\w*", "«иные варианты / материалы»: перечень не определён"),
    (r"\bи\s+т\.\s?п\.|\bи\s+т\.\s?д\.|\bи\s+др\.", "открытый перечень («и т. п.»)"),
]

_SENT = re.compile(r"(?<=[^\d\s][.;!?])\s+|\n+")   # «пункт 6.34. Технического задания» — не конец


def _text(it: dict) -> str:
    title, body = (it.get("title") or "").strip(), (it.get("text") or "").strip()
    return body if body.startswith(title) else f"{title}. {body}".strip(". ")


def _short(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip(" ,;:") + "…"


def _kinds(text: str) -> list[tuple[str, str]]:
    out = []
    for k, rx, label in KINDS:
        m = re.search(rx, text, re.I)
        if m:
            out.append((k, m.group(0)[:1].upper() + m.group(0)[1:] if k == "doc" else label))
    return out


def _refs(text: str, own: str) -> list[str]:
    """Пункты, на которые ссылается текст («пункт 6.34. Технического задания», «пункт 6.13.1 и 6.13.2»)."""
    out = []
    for m in REF.finditer(text):
        for n in re.findall(r"\d+(?:\.\d+)+", m.group(1)):
            if n != own and n not in out:
                out.append(n)
    return out


def _counts(text: str) -> list[str]:
    out = []
    for rx in COUNTS:
        for m in rx.finditer(text):
            g = [x for x in m.groups() if x]
            words = " ".join(x.strip() for x in g)
            words = re.sub(r"\s+", " ", words).strip()
            words = words[0].upper() + words[1:]
            if words not in out:
                out.append(words)
    return out


def _forms(text: str) -> list[str]:
    out = []
    for rx, label in FORMS:
        m = re.search(rx, text, re.I)
        if m:
            v = label.format(*(x.upper() if x else x for x in m.groups())) if "{0}" in label else label
            if v not in out:
                out.append(v)
    return out


def _parents(items: list[dict]) -> list[int | None]:
    """Родитель каждого пункта по порядку и уровню (как в pipeline.parents)."""
    stack: list[tuple[int, int]] = []
    out: list[int | None] = []
    for i, it in enumerate(items):
        lvl = int(it.get("level") or 1)
        while stack and stack[-1][1] >= lvl:
            stack.pop()
        out.append(stack[-1][0] if stack else None)
        stack.append((i, lvl))
    return out


def _trigger(text: str) -> str:
    """Предложение, по которому пункт попал в реестр (для столбца «Комментарий»)."""
    for sent in _SENT.split(text):
        if (DELIVER.search(sent) and _kinds(sent)) or STRONG.search(sent):
            return sent
    return ""


def analyze(items: list[dict], with_equipment: bool = False) -> dict:
    """Пункты ТЗ (num, title, text, level) -> {"rows", "clarify", "summary", "model", "usage"}."""
    items = [it for it in items if (it.get("title") or it.get("text"))]
    parents = _parents(items)

    def chain(i):
        while i is not None:
            yield items[i]
            i = parents[i]

    rows, clarify, sections = [], [], []
    for i, it in enumerate(items):
        text = _text(it)
        anc = list(chain(parents[i]))
        top = anc[-1] if anc else it
        section = (top.get("title") or "").strip()
        in_report = any(REPORT_SECTION.search(a.get("title") or "") for a in anc)
        is_leaf = i + 1 >= len(items) or int(items[i + 1].get("level") or 1) <= int(it.get("level") or 1)
        kinds = _kinds(text)
        trig = _trigger(text)
        reason = ""
        if in_report and (is_leaf or kinds):
            reason = "раздел об отчётности"
        elif trig:
            reason = "основание: «" + _short(trig, 150) + "»"
        elif with_equipment and is_leaf and EQUIPMENT.search(text) and QTY.search(text):
            kinds = [("photo", "Фото")]
            reason = "оснащение с количествами — подтверждение фото (в ТЗ не прописано)"
        if not reason:
            continue
        counts, forms = _counts(text), _forms(text)
        if in_report:
            first = _SENT.split(text)[0] if text else ""
            # короткое начало («Материалы, в т.ч.», «Отчёт … включающий:») — берём текст пункта целиком
            required = _short(first if len(first) >= 60 and not first.rstrip().endswith(":") else text, 220)
        else:
            parts = counts or [label for _k, label in kinds] or ["Фото"]
            required = "; ".join(dict.fromkeys(parts))
        if forms and not in_report:
            required += " — " + ", ".join(forms)
        num = (it.get("num") or "").strip().rstrip(".")
        if in_report:                      # «6.12, 7.1.9»: сдача по разделу отчётности + исходный пункт
            num = ", ".join(_refs(text, num) + [num])
        rows.append({"num": num, "section": section,
                     "name": _short(it.get("title") or text, 90), "description": _short(text, 420),
                     "required": required, "comment": reason})
        if section and section not in sections:
            sections.append(section)
        for rx, why in VAGUE:
            if re.search(rx, text, re.I):
                clarify.append({"num": rows[-1]["num"], "section": section, "name": rows[-1]["name"],
                                "description": _short(text, 300), "reason": why})
                break
    summary = {"analyzed": f"{len(items)} пунктов и подпунктов ТЗ; в реестре {len(rows)}, на уточнение {len(clarify)}",
               "sections": sections,
               "issues": ""}                       # «проверьте строки» — в строке статуса реестра
    return {"rows": rows, "clarify": clarify, "summary": summary, "model": "правила", "usage": {}}
