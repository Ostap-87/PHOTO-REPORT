"""Раскладка фото по пунктам ТЗ: zero-shot по тексту пункта + k-NN по фото, подтверждённым человеком.

Как считается вероятность пункта j для фото x (все векторы L2-нормированы, пространство CLIP):

1. Текст. Для пункта строится несколько формулировок (build_prompts), их эмбеддинги усредняются
   (prompt ensembling, как в CLIP) -> t_j. Логит z_j = cos(x, t_j) / temperature
   (temperature 0.01 = logit scale 100, как у обученного CLIP).
2. Примеры. Если у пункта есть подтверждённые фото E_j (n_j шт.), берём s_j — среднее из top-k
   косинусов x с E_j — и добавляем к логиту бонус
       knn_scale * w_j * (s_j - ref(x)),   w_j = n_j / (n_j + knn_n0),
   где ref(x) — «типичное сходство» этого фото с любыми фото проекта (среднее косинусов с
   примерами всех пунктов и с остальными фото пакета, не ниже knn_ref). Бонус бывает и
   отрицательным: фото, непохожее на примеры пункта, от него отталкивается.
3. softmax по пунктам -> вероятности. Пункт ставится, если p_top >= threshold и
   p_top - p_second >= margin, иначе item_id = None («Неразобранное»); подсказки — top-3.

Мини-бенчмарк (8 пунктов ТЗ на русском: банкетные столы, сцена, входная группа, серверная, покраска
стен, кресла в зале, фуршет, парковка; 70 открытых фото), значения по умолчанию:
* zero-shot: top-1 70 %; при threshold 0.6 / margin 0.2 раскладывается 61 % фото с точностью 91 %,
  39 % уходит в «Неразобранное» (с подсказками). Подсказка пункта (hint) поднимает top-1 до ~81 %;
* 2 подтверждённых фото на папку: top-1 92 %, раскладывается 95 % с точностью 95 %;
  1 фото — 86 %, 5 фото — 96 %;
* примеры только в половине папок: эти папки — 92 %, остальные — 65 % (без примеров было ~70 %).
Слабое место — абстрактные названия («Брендирование входной группы»: 14 % zero-shot); помогают
подсказка «что на фото» и 1–2 фото, положенные человеком.
"""
from __future__ import annotations

import re
from typing import Mapping

import numpy as np

DEFAULTS: dict = {
    "threshold": 0.6,     # минимальная вероятность лучшего пункта (0.5 — смелее: 71 % фото при точности 82 %)
    "margin": 0.2,        # отрыв лучшего пункта от второго
    "temperature": 0.01,  # softmax(cos / T); 0.01 = logit scale 100, как в CLIP
    "knn_k": 3,           # сколько ближайших примеров пункта усреднять
    "knn_scale": 50.0,    # сила бонуса от примеров (в единицах логита на 1.0 косинуса)
    "knn_n0": 1.0,        # w = n / (n + n0): 1 пример -> 0.5, 2 -> 0.67, 5 -> 0.83
    "knn_ref": 0.55,      # нижняя граница «типичного сходства» фото
    "top": 3,             # сколько подсказок отдавать
}

# --- очистка текста пункта ---
_NUM_PREFIX = re.compile(r"^\s*(?:п\.|пункт|№)?\s*(?:\d+[.)]?)+(?:\s*[.)\-–—:])?\s+", re.I)
_UNITS = (r"шт|штук[аи]?|компл(?:ект(?:а|ов)?)?|ед(?:иниц[аы]?)?|пар[аы]?|чел(?:овек)?|усл\.?\s*ед|"
          r"рейс(?:а|ов)?|смен[аы]?|час(?:а|ов)?|ч|дн(?:я|ей)|сут(?:ок|ки)?|руб(?:лей|ля)?|₽|позици(?:я|и|й)|"
          r"мест[ао]?|экз|набор(?:а|ов)?|услуг[аи]?")
_QTY = re.compile(
    r"[\s,;:(\-–—]*(?:в\s+количестве|кол-?во|количество)?\s*[:\-]?\s*\d+(?:[.,]\d+)?\s*(?:" + _UNITS + r")\.?(?=$|[\s,;:)])\)?",
    re.I)
_QTY_WORD = re.compile(r"\s*[,(]?\s*(?:в\s+количестве|кол-?во|количество)\s*[:\-]?\s*\d+[^,;)]*\)?", re.I)
_SPACES = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[.!?;])\s+|\n+")


def clean_text(s: str) -> str:
    """Убирает номер пункта и количества («12 шт», «в количестве 3 компл.»); размеры («6×4 м») оставляет."""
    s = str(s or "").replace(" ", " ").strip()
    s = _NUM_PREFIX.sub("", s)
    s = _QTY_WORD.sub("", s)
    s = _QTY.sub("", s)
    s = re.sub(r"\(\s*\)", "", s)
    s = _SPACES.sub(" ", s).strip(" .,;:-–—")
    return s


def _lower_first(s: str) -> str:
    # «Монтаж сцены» -> «монтаж сцены», но аббревиатуры («LED-экран», «СКС») не трогаем
    if len(s) > 1 and s[0].isupper() and not s[1].isupper():
        return s[0].lower() + s[1:]
    return s


# Шаблоны формулировок. Подобраны на бенчмарке (8 пунктов ТЗ, 70 фото): одиночные шаблоны дают
# top-1 0.60–0.70 («Снимок: …» и «Фотография: …» лучше, «на фото …» и голый заголовок хуже);
# набор ниже — 0.70 (прежний «фото: / … / на фото …» — 0.64), с подсказками — 0.81.
TITLE_TEMPLATES = ("Фотография: {lc}", "фото: {s}", "Снимок: {lc}")
TEXT_TEMPLATES = ("Фотография: {lc}",)


def build_prompts(item: Mapping) -> list[str]:
    """Формулировки для zero-shot по пункту ТЗ: из title, hint и первой фразы text."""
    title = clean_text(item.get("title", ""))
    hint = clean_text(item.get("hint", ""))
    text = str(item.get("text", "") or "")
    first = ""
    if text:
        parts = [p for p in _SENT.split(text.strip()) if p.strip()]
        first = clean_text(parts[0]) if parts else ""
        if len(first) > 160:
            first = first[:160].rsplit(" ", 1)[0]
    out: list[str] = []
    for base in (title, hint):
        if base:
            out += [t.format(s=base, lc=_lower_first(base)) for t in TITLE_TEMPLATES]
    if first and first.lower() not in (title.lower(), hint.lower()) and not title.lower().startswith(first.lower()):
        out += [t.format(s=first, lc=_lower_first(first)) for t in TEXT_TEMPLATES]
    if not out:
        num = str(item.get("num", "") or "").strip()
        out = [f"пункт {num}".strip()]
    seen, res = set(), []
    for p in out:
        k = p.lower()
        if k not in seen:
            seen.add(k)
            res.append(p)
    return res


REF_SAMPLE = 1000  # сколько фото пакета брать для «типичного сходства» ref(x)


def _norm(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        x = x[None]
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-12)


def _softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def scores(photo_embs, item_text_embs: Mapping[str, np.ndarray], examples: Mapping[str, np.ndarray] | None,
           params: Mapping | None = None) -> tuple[list[str], np.ndarray]:
    """Вероятности по пунктам: (ids, P (N×M)). Основа для classify()."""
    p = {**DEFAULTS, **(params or {})}
    X = _norm(photo_embs) if len(photo_embs) else np.zeros((0, 1), np.float32)
    examples = {k: v for k, v in (examples or {}).items() if v is not None and len(v)}
    ids = list(item_text_embs.keys()) + [k for k in examples if k not in item_text_embs]
    N, M = len(X), len(ids)
    if N == 0 or M == 0:
        return ids, np.zeros((N, M), np.float32)

    # 1) текст: усреднённый эмбеддинг формулировок пункта
    cos_t = np.zeros((N, M), np.float32)
    has_t = np.zeros(M, bool)
    for j, iid in enumerate(ids):
        t = item_text_embs.get(iid)
        if t is not None and len(t):
            c = _norm(_norm(t).mean(0))[0]
            cos_t[:, j] = X @ c
            has_t[j] = True
    if not has_t.all():  # пункты только с примерами — нейтральный текстовый логит
        fill = cos_t[:, has_t].mean(1) if has_t.any() else np.zeros(N, np.float32)
        cos_t[:, ~has_t] = fill[:, None]
    logits = cos_t / float(p["temperature"])

    # 2) примеры, подтверждённые человеком
    if examples:
        all_ex = _norm(np.concatenate([np.asarray(v, np.float32) for v in examples.values()]))
        # опорный набор: все примеры + до REF_SAMPLE остальных фото пакета (память N×1000, а не N×N)
        others = X
        if N > REF_SAMPLE:
            others = X[np.random.default_rng(0).choice(N, REF_SAMPLE, replace=False)]
        pooled = np.concatenate([X @ all_ex.T, X @ others.T], axis=1) if N > 1 else X @ all_ex.T
        valid = pooled <= 0.97                                      # дубли и само фото не в счёт
        cnt = valid.sum(1)
        ref = np.where(valid, pooled, 0.0).sum(1) / np.maximum(cnt, 1)
        ref = np.where(cnt > 0, np.maximum(ref, p["knn_ref"]), p["knn_ref"]).astype(np.float32)
        k = int(p["knn_k"])
        for j, iid in enumerate(ids):
            E = examples.get(iid)
            if E is None:
                continue
            E = _norm(E)
            s = X @ E.T
            kk = min(k, s.shape[1])
            top = np.sort(s, axis=1)[:, -kk:].mean(1)
            n = E.shape[0]
            w = n / (n + float(p["knn_n0"]))
            logits[:, j] += float(p["knn_scale"]) * w * (top - ref)
    return ids, _softmax(logits)


def classify(photo_embs, item_text_embs: Mapping[str, np.ndarray], examples: Mapping[str, np.ndarray] | None,
             params: Mapping | None = None) -> list[dict]:
    """-> [{"item_id": str | None, "confidence": float, "suggestions": [[item_id, p], ...]}] на каждое фото.

    photo_embs — (N,512) эмбеддинги фото; item_text_embs — {item_id: (P,512) эмбеддинги формулировок
    build_prompts(item)}; examples — {item_id: (K,512) эмбеддинги фото, которые человек положил в
    этот пункт}; params — переопределения DEFAULTS (threshold, margin, temperature, knn_*).
    """
    p = {**DEFAULTS, **(params or {})}
    ids, P = scores(photo_embs, item_text_embs, examples, p)
    out: list[dict] = []
    top_n = int(p["top"])
    for row in P:
        if len(ids) == 0:
            out.append({"item_id": None, "confidence": 0.0, "suggestions": []})
            continue
        order = np.argsort(-row)
        best = float(row[order[0]])
        second = float(row[order[1]]) if len(order) > 1 else 0.0
        ok = best >= float(p["threshold"]) and (best - second) >= float(p["margin"])
        out.append({
            "item_id": ids[order[0]] if ok else None,
            "confidence": round(best, 4),
            "suggestions": [[ids[i], round(float(row[i]), 4)] for i in order[:top_n]],
        })
    return out
