"""Улучшение фото классическими методами OpenCV (ничего не «дорисовывает»).

Возможные шаги (их имена попадают в список applied):
  white_balance — баланс белого (оценка освещения shades of gray, частичная коррекция);
  exposure      — экспозиция: усиление до «белой точки» + гамма по медиане (тёмные) или
                  гамма > 1 для пересвеченных;
  levels        — растяжение яркости по перцентилям (низкий контраст);
  clahe         — локальный контраст CLAHE по каналу L в LAB;
  denoise       — мягкое шумоподавление (после сильного осветления или при заметном шуме);
  sharpen       — нерезкое маскирование по каналу L (лёгкая размытость).

mode="auto" — только то, что нужно по оценке качества (нормальные фото не меняются);
mode="all"  — то же плюс мягкая общая коррекция для всех фото; mode="off" — без изменений.
"""
from __future__ import annotations

import cv2
import numpy as np

from . import quality as Q

# --- экспозиция ---
TARGET_MEDIAN = 0.40     # к этой медиане яркости тянем тёмные фото (типично для нормальных 0.35–0.5)
MIN_GAMMA = 0.40         # предел осветления гаммой (сильнее — «пластик» и шум)
LOWKEY_HI = 0.85         # в кадре есть яркие источники (p99.5 >= 0.85): это тёмная сцена (концерт, ночь,
LOWKEY_TARGET = 0.28     # паркинг с лампами), а не недодержка — осветляем мягче, иначе чёрное
LOWKEY_MIN_GAMMA = 0.60  # становится серой дымкой, а шум лезет наружу
WHITE_POINT = 0.97       # усиление ограничено: p99.5 яркости не выше этого уровня
MAX_GAIN = 4.0
OVER_TARGET = 0.55       # пересвеченные: медиану опускаем к этому уровню
MAX_GAMMA = 1.6
# --- контраст ---
LEVELS_LO, LEVELS_HI = 0.5, 99.5   # перцентили для растяжения
CLAHE_CLIP = 2.0
CLAHE_TILES = 8
CLAHE_BLEND = 0.6        # CLAHE подмешиваем частично — без «HDR-эффекта»
# --- цвет ---
WB_STRENGTH = 0.6        # доля коррекции баланса белого (1.0 — полный «серый мир», часто перебор)
WB_MAX_GAIN = 1.25       # не больше ±25 % на канал: тёплый свет вывески/заката не превращается в бирюзу
# --- шум и резкость ---
DENOISE_AFTER_GAIN = 3.0 # тени усилены сильнее этого (экспозиция × растяжение) -> шумоподавление
DENOISE_NOISE = 3.0      # ...или ожидаемый шум после усиления (σ × усиление) выше этого
SHARPEN_AMOUNT = 1.2     # подобрано по SSIM на размытых копиях (σ=1.2 px: 0.889->0.925, σ=2: 0.802->0.836)
SHARPEN_SIGMA_1024 = 1.5 # радиус маски на масштабе 1024 px; для больших фото — пропорционально, до 4 px
SHARPEN_THRESHOLD = 2.0  # не усиливать мелочь слабее 2 уровней (шум)
# --- мягкая общая коррекция (mode="all") ---
ALL_LEVELS_STRENGTH = 0.3
ALL_CLAHE_BLEND = 0.15
ALL_WB_STRENGTH = 0.3
ALL_WB_MIN_CAST = 6.0
ALL_SHARPEN_AMOUNT = 0.35
ALL_SHARPEN_SIGMA = 1.0


def _lut_apply(rgb: np.ndarray, lut: np.ndarray) -> np.ndarray:
    lut = np.clip(lut + 0.5, 0, 255).astype(np.uint8)
    return cv2.LUT(rgb, lut)


def _luma_stats(rgb: np.ndarray) -> tuple[float, float, float]:
    """(медиана, p0.5, p99.5) яркости 0..1 по уменьшенной копии."""
    y = cv2.cvtColor(Q._small(rgb), cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    lo, hi = np.percentile(y, [LEVELS_LO, LEVELS_HI])
    return float(np.median(y)), float(lo), float(hi)


def exposure(rgb: np.ndarray, dark: bool, over: bool) -> tuple[np.ndarray, float]:
    """Коррекция экспозиции. Возвращает (rgb, во сколько раз осветлены тени)."""
    med, _, hi = _luma_stats(rgb)
    x = np.arange(256, dtype=np.float32) / 255.0
    shadow_gain = 1.0
    if dark:
        # 1) усиление (как при правильной выдержке), пока светлые участки не упрутся в белое
        gain = float(np.clip(WHITE_POINT / max(hi, 1e-3), 1.0, MAX_GAIN))
        med_g = min(0.999, med * gain)
        # 2) гамма по медиане — если после усиления всё ещё темно
        lowkey = hi >= LOWKEY_HI
        target = LOWKEY_TARGET if lowkey else TARGET_MEDIAN
        gamma = 1.0
        if med_g < target * 0.9 and med_g > 1e-3:
            gamma = float(np.clip(np.log(target) / np.log(med_g), LOWKEY_MIN_GAMMA if lowkey else MIN_GAMMA, 1.0))
        y = np.clip(x * gain, 0, 1) ** gamma
        # во сколько раз выросли тени (уровень 0.1) — для решения о шумоподавлении
        shadow_gain = float(y[26] / max(x[26], 1e-6))
        return _lut_apply(rgb, y * 255.0), shadow_gain
    if over and med > OVER_TARGET:
        gamma = float(np.clip(np.log(OVER_TARGET) / np.log(min(max(med, 1e-3), 0.995)), 1.0, MAX_GAMMA))
        return _lut_apply(rgb, (x ** gamma) * 255.0), 1.0
    return rgb, 1.0


def levels(rgb: np.ndarray, strength: float = 1.0) -> tuple[np.ndarray, float]:
    """Растяжение яркости по перцентилям (одинаково для всех каналов — цвета не плывут).
    Возвращает (rgb, во сколько раз растянуты перепады)."""
    _, lo, hi = _luma_stats(rgb)
    if hi - lo < 0.05:
        return rgb, 1.0
    x = np.arange(256, dtype=np.float32) / 255.0
    y = np.clip((x - lo) / (hi - lo), 0, 1)
    y = x + (y - x) * strength
    return _lut_apply(rgb, y * 255.0), 1.0 + (1.0 / (hi - lo) - 1.0) * strength


def clahe(rgb: np.ndarray, clip: float = CLAHE_CLIP, blend: float = CLAHE_BLEND) -> np.ndarray:
    """CLAHE по каналу L (LAB) с частичным подмешиванием."""
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    L = lab[..., 0]
    c = cv2.createCLAHE(clipLimit=clip, tileGridSize=(CLAHE_TILES, CLAHE_TILES))
    L2 = c.apply(L)
    if blend < 1.0:
        L2 = cv2.addWeighted(L2, blend, L, 1.0 - blend, 0)
    lab[..., 0] = L2
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def white_balance(rgb: np.ndarray, strength: float = WB_STRENGTH) -> np.ndarray:
    """Баланс белого: оценка освещения shades of gray (p=6) в линейном свете, частичная коррекция."""
    _, est = Q.cast_angle(Q._small(rgb))
    est = np.asarray(est, np.float32)
    if not np.all(est > 0):
        return rgb
    gains = (1.0 / est) ** strength
    # сохраняем яркость: нормируем по весам яркости Rec.709
    gains = gains / float(np.dot(gains, [0.2126, 0.7152, 0.0722]))
    gains = np.clip(gains, 1 / WB_MAX_GAIN, WB_MAX_GAIN)
    x = np.arange(256, dtype=np.float32) / 255.0
    lin = x ** 2.2
    out = np.empty_like(rgb)
    for ch in range(3):
        y = np.clip(lin * gains[ch], 0, 1) ** (1 / 2.2)
        out[..., ch] = cv2.LUT(rgb[..., ch], np.clip(y * 255 + 0.5, 0, 255).astype(np.uint8))
    return out


def denoise(rgb: np.ndarray, strength: float = 1.0) -> np.ndarray:
    """Мягкое шумоподавление: сильнее по цвету (цветные пятна в тенях), слабее по яркости."""
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    h, w = rgb.shape[:2]
    s = max(1.0, max(h, w) / 1024.0)
    d = int(round(5 * min(s, 3)))
    L = cv2.bilateralFilter(lab[..., 0], d, 8 * strength, 2 * s)
    k = int(2 * round(1.5 * s) + 1)
    a = cv2.GaussianBlur(lab[..., 1], (k, k), 0)
    b = cv2.GaussianBlur(lab[..., 2], (k, k), 0)
    return cv2.cvtColor(cv2.merge([L, a, b]), cv2.COLOR_LAB2RGB)


def sharpen(rgb: np.ndarray, amount: float = SHARPEN_AMOUNT, sigma_1024: float | None = None) -> np.ndarray:
    """Нерезкое маскирование по каналу L; радиус — под разрешение фото."""
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    L = lab[..., 0].astype(np.float32)
    h, w = rgb.shape[:2]
    s1024 = SHARPEN_SIGMA_1024 if sigma_1024 is None else sigma_1024
    sigma = float(np.clip(s1024 * max(h, w) / 1024.0, 0.8, 4.0))
    blur = cv2.GaussianBlur(L, (0, 0), sigma)
    detail = L - blur
    detail = np.where(np.abs(detail) < SHARPEN_THRESHOLD, 0.0, detail)
    lab[..., 0] = np.clip(L + amount * detail, 0, 255).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def enhance(rgb: np.ndarray, quality: dict | None = None, mode: str = "auto") -> tuple[np.ndarray, list[str]]:
    """Улучшает фото. Возвращает (новый rgb, список применённых шагов). Исходный массив не меняется.

    quality — результат quality.analyze(rgb) (если None — посчитаем сами).
    """
    if mode not in ("auto", "all", "off"):
        raise ValueError("mode: auto | all | off")
    if mode == "off":
        return rgb, []
    q = quality if quality is not None else Q.analyze(rgb)
    flags = set(q.get("flags", []))
    if mode == "auto" and not q.get("needs_enhance"):
        return rgb, []

    out = np.ascontiguousarray(rgb[..., :3])
    applied: list[str] = []

    if "color_cast" in flags:
        out = white_balance(out, WB_STRENGTH)
        applied.append("white_balance")
    elif mode == "all" and float(q.get("cast", 0)) > ALL_WB_MIN_CAST:
        out = white_balance(out, ALL_WB_STRENGTH)
        applied.append("white_balance")

    amp = 1.0  # во сколько раз усилены перепады в тенях (растёт и шум)
    dark, over = "dark" in flags, "overexposed" in flags
    if dark or over:
        out, g = exposure(out, dark, over)
        amp *= g
        applied.append("exposure")

    if "low_contrast" in flags:
        out, g = levels(out, 1.0)
        amp *= g
        out = clahe(out, CLAHE_CLIP, CLAHE_BLEND)
        applied += ["levels", "clahe"]
    elif dark:
        out = clahe(out, CLAHE_CLIP, CLAHE_BLEND * 0.6)  # после осветления — немного локального контраста
        applied.append("clahe")
    elif mode == "all":
        out, _ = levels(out, ALL_LEVELS_STRENGTH)
        out = clahe(out, 1.5, ALL_CLAHE_BLEND)
        applied += ["levels", "clahe"]

    noise_after = float(q.get("noise", 0)) * amp
    if amp >= DENOISE_AFTER_GAIN or (amp >= 1.5 and noise_after > DENOISE_NOISE):
        out = denoise(out, 1.0 if noise_after < 2 * DENOISE_NOISE else 1.5)
        applied.append("denoise")

    if "blurry" in flags and "very_blurry" not in flags:
        out = sharpen(out, SHARPEN_AMOUNT)
        applied.append("sharpen")
    elif mode == "all" and "denoise" not in applied:
        out = sharpen(out, ALL_SHARPEN_AMOUNT, ALL_SHARPEN_SIGMA)
        applied.append("sharpen")

    return out, applied
