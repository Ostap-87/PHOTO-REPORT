"""Раскладка фото по пунктам ТЗ: zero-shot по тексту пункта + k-NN по фото, подтверждённым человеком.

Как считается вероятность пункта j для фото x (все векторы L2-нормированы, пространство CLIP):

1. Текст. Для пункта строится несколько формулировок (build_prompts), их эмбеддинги усредняются
   (prompt ensembling, как в CLIP) -> t_j. Логит z_j = cos(x, t_j) / temperature
   (temperature 0.01 = logit scale 100, как у обученного CLIP).
2. Примеры. Если у пункта есть подтверждённые фото E_j (n_j шт.), берём s_j — среднее из top-k
   косинусов x с E_j — и добавляем к логиту бонус
       knn_scale * w_j * (s_j - ref(x)),   w_j = n_j / (n_j + knn_n0),
   где ref(x) — «типичное сходство» этого фото с любыми фото проекта (среднее косинусов с
   примерами всех пунктов и с остальными фото пакета; при малом пакете — не ниже knn_ref).
   Так пункты с примерами не перетягивают на себя всё подряд, а вес примеров растёт с их числом.
3. softmax по пунктам -> вероятности. Пункт ставится, если p_top >= threshold и
   p_top - p_second >= margin, иначе item_id = None («Неразобранное»); подсказки — top-3.

Значения по умолчанию (DEFAULTS) подобраны на мини-бенчмарке: 8 пунктов ТЗ на русском
(столы, сцена, вход, серверная, покраска, кресла, фуршет, парковка), открытые фото — см. отчёт.
"""
from __future__ import annotations

import re
from typing import Mapping

import numpy as np

DEFAULTS: dict = {
    "threshold": 0.45,    # минимальная вероятность лучшего пункта
    "margin": 0.15,       # отрыв лучшего пункта от второго
    "temperature": 0.01,  # softmax(cos / T); 0.01 = logit scale 100, как в CLIP
    "knn_k": 3,           # сколько ближайших примеров пункта усреднять
    "knn_scale": 40.0,    # сила бонуса от примеров (в единицах логита на 1.0 косинуса)
    "knn_n0": 2.0,        # w = n / (n + n0): 1 пример -> 0.33, 2 -> 0.5, 6 -> 0.75
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


def build_prompts(item: Mapping) -> list[str]:
    """Формулировки для zero-shot по пункту ТЗ: из title, hint и начала text."""
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
            out += [f"фото: {base}", base, f"на фото {_lower_first(base)}"]
    if first and first.lower() not in (title.lower(), hint.lower()) and not title.lower().startswith(first.lower()):
        out += [first, f"фото: {first}"]
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
        sims_all = X @ all_ex.T                                     # N×E
        pool = [sims_all]
        if N > 1:
            pp = X @ X.T
            np.fill_diagonal(pp, np.nan)
            pool.append(pp)
        pooled = np.concatenate(pool, axis=1)
        pooled = np.where(pooled > 0.97, np.nan, pooled)            # дубли и само фото не в счёт
        with np.errstate(all="ignore"):
            ref = np.nanmean(pooled, axis=1)
        ref = np.where(np.isnan(ref), p["knn_ref"], np.maximum(ref, p["knn_ref"]))
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
