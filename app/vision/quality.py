"""Оценка качества фото: экспозиция, контраст, резкость, шум, пересвет/провалы, цветовой сдвиг.

Все измерения делаются на уменьшенной копии (длинная сторона ANALYZE_SIDE), поэтому значения
сопоставимы для фото разного разрешения (телефон 12 Мп и превью 800 px дают близкие цифры).
Пороги ниже подобраны на реальных фото (Wikimedia Commons: залы, сцены, серверные, фуршеты,
парковки, ремонт — 60 шт.) и на их искусственно испорченных копиях (затемнение, пересвет,
снижение контраста, размытие, цветовой сдвиг); подробности — в комментариях у каждого порога.
"""
from __future__ import annotations

import cv2
import numpy as np

ANALYZE_SIDE = 1024        # длинная сторона копии для анализа, px

# --- экспозиция (яркость = средняя яркость Y, доля 0..1) ---
DARK_MEAN = 0.24           # хорошие фото: среднее 0.30–0.65; затемнение ×0.5 даёт 0.12–0.25
DARK_MEDIAN = 0.18         # ...и медиана ниже — иначе это просто тёмный фон при светлом объекте
VERY_DARK_MEDIAN = 0.025   # почти чёрный кадр — улучшением не спасти (reshoot)
OVER_CLIP = 0.12           # доля пересвеченных пикселей (Y >= 250); у хороших фото обычно < 0.03
OVER_MEAN = 0.62           # ...и общая яркость высокая (белая стена в кадре сама по себе не пересвет)
OVER_MEDIAN = 0.80         # или медиана очень высокая — «выбеленный» кадр без явного клиппинга
HOPELESS_CLIP = 0.55       # больше половины кадра выбито в белое — reshoot

# --- контраст ---
LOW_CONTRAST_STD = 0.115   # RMS-контраст (std(Y)); хорошие фото 0.17–0.30, «серые» копии 0.06–0.11
LOW_CONTRAST_SPREAD = 0.50 # размах яркости p99–p1; у хороших фото 0.8–1.0

# --- резкость: дисперсия лапласиана на копии 1024 px после растяжения яркости p1–p99 ---
BLURRY = 90.0              # хорошие фото: 300–6000; размытие σ=2 px даёт 30–120, σ=3 — 10–50
VERY_BLURRY = 28.0         # ниже — расфокус/смаз, повышением резкости не исправить
EDGE_SHARP_MIN = 0.16      # доп. признак: сила самых резких перепадов (p99.5 градиента / размах);
                           # у резких фото 0.25+, у размытых < 0.15 — защищает от ложного «размыто»
                           # на однотонных сценах (стена, небо) с малой дисперсией лапласиана

# --- цвет: отклонение оценки освещения (shades of gray, p=6) от нейтрального, градусы ---
COLOR_CAST_DEG = 7.0       # хорошие фото: 0.5–6°; лампы накаливания/сдвиг ±20% по каналу: 8–20°

# --- шум (σ в уровнях 0..255 на копии 1024 px, оценка по медиане отклика лапласиан-фильтра) ---
NOISY = 6.0                # выше — заметный шум (для решения о шумоподавлении после осветления)


def _small(rgb: np.ndarray, side: int = ANALYZE_SIDE) -> np.ndarray:
    h, w = rgb.shape[:2]
    s = side / max(h, w)
    if s >= 1:
        return rgb
    return cv2.resize(rgb, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)


def _luma(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def sharpness(gray: np.ndarray) -> tuple[float, float]:
    """(дисперсия лапласиана после нормировки яркости, сила резких перепадов 0..1)."""
    g = gray.astype(np.float32)
    lo, hi = np.percentile(g, [1, 99])
    span = max(hi - lo, 1.0)
    gn = np.clip((g - np.float32(lo)) * np.float32(255.0 / span), 0, 255).astype(np.float32)
    # лёгкое сглаживание, чтобы шум матрицы не выдавал себя за резкость
    gn = cv2.GaussianBlur(gn, (3, 3), 0.6)
    lap = cv2.Laplacian(gn, cv2.CV_32F, ksize=1)
    lapvar = float(lap.var())
    gx = cv2.Sobel(gn, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gn, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    edge = float(np.percentile(mag, 99.5)) / (4.0 * 255.0)  # 4*255 — максимум Собеля для ступеньки
    return lapvar, edge


def noise_sigma(gray: np.ndarray) -> float:
    """Оценка σ шума (Immerkær, устойчивая версия через медиану)."""
    k = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
    r = cv2.filter2D(gray.astype(np.float32), -1, k, borderType=cv2.BORDER_REFLECT)[1:-1, 1:-1]
    return float(np.median(np.abs(r)) / (0.6745 * 6.0))


def cast_angle(rgb: np.ndarray) -> tuple[float, list[float]]:
    """Угол (°) между оценкой цвета освещения и серым + сама оценка (R,G,B, нормированы к G=1)."""
    x = rgb.reshape(-1, 3).astype(np.float32) / 255.0
    lum = x.mean(1)
    m = (lum > 0.06) & (x.max(1) < 0.97)        # без провалов и пересвета
    if m.sum() < 500:
        return 0.0, [1.0, 1.0, 1.0]
    lin = np.power(x[m], 2.2)                     # приблизительно линейный свет
    est = np.power(np.mean(lin ** 6, axis=0), 1 / 6)
    est = est / max(est[1], 1e-9)
    cos = float(est.sum() / (np.linalg.norm(est) * np.sqrt(3)))
    ang = float(np.degrees(np.arccos(np.clip(cos, -1, 1))))
    return ang, [round(float(v), 4) for v in est]


def analyze(rgb: np.ndarray) -> dict:
    """Оценка качества. Возвращает словарь (все числа — обычные float/bool, готово для JSON):

    brightness — средняя яркость 0..1; median — медиана яркости; contrast — RMS-контраст (std) 0..1;
    spread — размах яркости p99–p1; blur — дисперсия лапласиана (больше = резче); edge — сила перепадов;
    noise — σ шума; clip_high / clip_low — доли пересвета (Y>=250) и провалов (Y<=5);
    cast — цветовой сдвиг в градусах; flags — список из dark, overexposed, low_contrast, blurry,
    very_blurry, color_cast; needs_enhance — есть что улучшать; reshoot — переснять (не спасти).
    """
    small = _small(np.ascontiguousarray(rgb[..., :3]))
    y = _luma(small)
    yf = y.astype(np.float32) / 255.0
    mean = float(yf.mean())
    median = float(np.median(yf))
    std = float(yf.std())
    p1, p99 = (float(v) for v in np.percentile(yf, [1, 99]))
    clip_high = float((y >= 250).mean())
    clip_low = float((y <= 5).mean())
    lapvar, edge = sharpness(y)
    noise = noise_sigma(y)
    cast, illum = cast_angle(small)

    flags: list[str] = []
    if mean < DARK_MEAN and median < DARK_MEDIAN:
        flags.append("dark")
    if (clip_high > OVER_CLIP and mean > OVER_MEAN) or median > OVER_MEDIAN:
        flags.append("overexposed")
    if std < LOW_CONTRAST_STD or (p99 - p1) < LOW_CONTRAST_SPREAD:
        flags.append("low_contrast")
    blurry = lapvar < BLURRY and edge < EDGE_SHARP_MIN * 1.6
    very_blurry = lapvar < VERY_BLURRY and edge < EDGE_SHARP_MIN
    if blurry or very_blurry:
        flags.append("blurry")
    if very_blurry:
        flags.append("very_blurry")
    if cast > COLOR_CAST_DEG:
        flags.append("color_cast")

    reshoot = very_blurry or median < VERY_DARK_MEDIAN or clip_high > HOPELESS_CLIP
    fixable = {"dark", "overexposed", "low_contrast", "color_cast"}
    needs = bool(fixable.intersection(flags)) or (blurry and not very_blurry)
    return {
        "brightness": round(mean, 4),
        "median": round(median, 4),
        "contrast": round(std, 4),
        "spread": round(p99 - p1, 4),
        "blur": round(lapvar, 1),
        "edge": round(edge, 4),
        "noise": round(noise, 2),
        "clip_high": round(clip_high, 4),
        "clip_low": round(clip_low, 4),
        "cast": round(cast, 2),
        "illuminant": illum,
        "flags": flags,
        "needs_enhance": bool(needs),
        "reshoot": bool(reshoot),
    }
