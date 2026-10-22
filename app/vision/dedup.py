"""Поиск почти-дублей: dHash 64 бита + группировка по расстоянию Хэмминга."""
from __future__ import annotations

from typing import Mapping, Sequence

import cv2
import numpy as np

DEFAULT_MAX_DIST = 6   # из 64 бит: пересжатие/уменьшение/лёгкая правка яркости дают 0–4,
                       # соседние кадры серии 2–8, разные сцены — обычно 20+


def dhash(rgb: np.ndarray, size: int = 8) -> int:
    """Разностный хэш: серое (size+1)×size, сравнение соседних пикселей по строкам -> int (64 бита)."""
    img = np.ascontiguousarray(rgb[..., :3]) if rgb.ndim == 3 else rgb
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img
    h, w = gray.shape[:2]
    # сначала грубо уменьшаем большие фото (быстрее и без муара), потом — до 9×8
    if max(h, w) > 512:
        s = 512 / max(h, w)
        gray = cv2.resize(gray, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    small = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA).astype(np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def hamming(a: int, b: int) -> int:
    return int(bin((int(a) ^ int(b)) & ((1 << 64) - 1)).count("1"))


def _popcount64(x: np.ndarray) -> np.ndarray:
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(x)
    # запасной путь для старого numpy: по байтам через таблицу
    table = np.array([bin(i).count("1") for i in range(256)], np.uint8)
    return table[x.view(np.uint8).reshape(*x.shape, 8)].sum(-1)


def groups(hashes: Sequence[int], max_dist: int = DEFAULT_MAX_DIST) -> list[list[int]]:
    """Группы почти-дублей (только группы из 2+ фото), индексы — как в hashes, по возрастанию.

    Связность транзитивная (union-find): если A~B и B~C, то A, B, C в одной группе.
    """
    n = len(hashes)
    if n < 2:
        return []
    arr = np.array([int(h) & ((1 << 64) - 1) for h in hashes], dtype=np.uint64)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    chunk = 2048
    for s in range(0, n, chunk):
        block = arr[s:s + chunk]
        d = _popcount64(block[:, None] ^ arr[None, :])     # (chunk, n)
        ii, jj = np.nonzero(d <= max_dist)
        for i, j in zip((ii + s).tolist(), jj.tolist()):
            if j > i:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    out: dict[int, list[int]] = {}
    for i in range(n):
        out.setdefault(find(i), []).append(i)
    return sorted((g for g in out.values() if len(g) > 1), key=lambda g: g[0])


def best(group_indices: Sequence[int], blur_values: Sequence[float] | Mapping[int, float]) -> int:
    """Лучшее (самое резкое) фото группы -> его индекс из group_indices.

    blur_values — резкость (quality["blur"], больше = резче) по индексам общего списка
    (как hashes): список/массив или словарь {индекс: резкость}. Допускается и список,
    выровненный по самой группе (той же длины, когда индексы группы за его пределами).
    """
    idx = list(group_indices)
    if not idx:
        raise ValueError("пустая группа")
    if isinstance(blur_values, Mapping):
        vals = [float(blur_values.get(i, 0.0) or 0.0) for i in idx]
    else:
        bv = list(blur_values)
        if len(bv) == len(idx) and max(idx) >= len(bv):
            vals = [float(v or 0.0) for v in bv]
        else:
            vals = [float(bv[i] or 0.0) if i < len(bv) else 0.0 for i in idx]
    return idx[int(np.argmax(vals))]
