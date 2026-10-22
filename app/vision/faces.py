"""Поиск и скрытие лиц.

Детектор — OpenCV YuNet (``cv2.FaceDetectorYN``, модель ``models/face_detection_yunet_2023mar.onnx``,
~230 КБ, лицензия MIT, из opencv_zoo). YuNet уверенно видит лица размером примерно 10–300 px во
входной картинке, поэтому фото проверяется в нескольких масштабах: целиком (320, 640, 1600 px по
длинной стороне — крупные и средние лица) и, для больших фото, плитками в высоком разрешении
(мелкие лица в толпе). Рамки пересчитываются в координаты исходного фото и сводятся NMS.
Запасной вариант (если YuNet недоступен) — Haar-каскад из ``cv2.data.haarcascades``.
"""
from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np

MODEL_PATH = Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"

SCORE_THRESHOLD = 0.62   # уверенность YuNet; ниже 0.6 появляются ложные «лица» на узорах,
                         # выше 0.75 теряются мелкие лица в толпе (0.64–0.8) — для приватности лучше лишнее
LARGE_FACE = 0.15        # «крупное лицо»: сторона рамки >= 15 % короткой стороны фото...
LARGE_FACE_SCORE = 0.80  # ...для него нужна уверенность 0.8: настоящие крупные лица дают 0.83–0.95,
                         # а ложные крупные (канапе на блюде, вывеска MARKET) — 0.67–0.75
NMS_IOU = 0.3
WHOLE_SIDES = (320, 640, 1600)  # проходы по целому кадру
TILE = 1600              # размер плитки для мелких лиц
TILE_MAX_SIDE = 3200     # фото больше 1600 px режем на плитки на масштабе до 3200 px
TILE_OVERLAP = 0.2

PAD = 0.2                # запас вокруг рамки лица (с каждой стороны), доля размера
FEATHER = 0.12           # мягкость края овала, доля размера лица

_local = threading.local()


def _yunet():
    """YuNet на поток (детектор хранит размер входа — между потоками не делим)."""
    det = getattr(_local, "yunet", None)
    if det is None and not getattr(_local, "yunet_failed", False):
        try:
            if not MODEL_PATH.is_file() or not hasattr(cv2, "FaceDetectorYN"):
                raise FileNotFoundError(MODEL_PATH)
            det = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (320, 320), SCORE_THRESHOLD, NMS_IOU, 5000)
            _local.yunet = det
        except Exception:  # noqa: BLE001 — уходим на Haar
            _local.yunet_failed = True
            det = None
    return det


def _haar():
    det = getattr(_local, "haar", None)
    if det is None:
        det = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
        _local.haar = det
    return det


def backend() -> str:
    """Какой детектор используется: 'yunet' или 'haar'."""
    return "yunet" if _yunet() is not None else "haar"


def _resize(img: np.ndarray, side: int) -> tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    s = side / max(h, w)
    if abs(s - 1) < 1e-6:
        return img, 1.0
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
    return cv2.resize(img, (max(1, round(w * s)), max(1, round(h * s))), interpolation=interp), s


def _run_yunet(det, bgr: np.ndarray) -> np.ndarray:
    h, w = bgr.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(bgr)
    if faces is None:
        return np.zeros((0, 5), np.float32)
    return np.concatenate([faces[:, :4], faces[:, 14:15]], axis=1).astype(np.float32)


def detect(rgb: np.ndarray, score_threshold: float = SCORE_THRESHOLD) -> list[tuple[int, int, int, int, float]]:
    """Лица на фото -> [(x, y, w, h, score)] в координатах исходного изображения."""
    bgr = cv2.cvtColor(np.ascontiguousarray(rgb[..., :3]), cv2.COLOR_RGB2BGR)
    H, W = bgr.shape[:2]
    long_side = max(H, W)
    det = _yunet()
    found: list[np.ndarray] = []
    if det is not None:
        det.setScoreThreshold(float(score_threshold))
        # целый кадр на нескольких масштабах (без увеличения сверх исходного размера)
        for side in sorted({min(sd, long_side) for sd in WHOLE_SIDES}):
            img, s = _resize(bgr, side)
            f = _run_yunet(det, img)
            if len(f):
                f[:, :4] /= s
                found.append(f)
        if long_side > TILE:
            img, s = _resize(bgr, min(long_side, TILE_MAX_SIDE))
            h, w = img.shape[:2]
            step = int(TILE * (1 - TILE_OVERLAP))
            for y0 in range(0, max(1, h - int(TILE * TILE_OVERLAP)), step):
                for x0 in range(0, max(1, w - int(TILE * TILE_OVERLAP)), step):
                    tile = img[y0:y0 + TILE, x0:x0 + TILE]
                    if min(tile.shape[:2]) < 64:
                        continue
                    f = _run_yunet(det, np.ascontiguousarray(tile))
                    if len(f):
                        f[:, 0] += x0
                        f[:, 1] += y0
                        f[:, :4] /= s
                        found.append(f)
    else:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        img, s = _resize(gray, min(long_side, 1600))
        img = cv2.equalizeHist(img)
        rects = _haar().detectMultiScale(img, scaleFactor=1.1, minNeighbors=5, minSize=(20, 20))
        if len(rects):
            f = np.asarray(rects, np.float32)
            f = np.concatenate([f / s, np.full((len(f), 1), 0.5, np.float32)], axis=1)
            found.append(f)

    if not found:
        return []
    allf = np.concatenate(found)
    boxes = [[float(x), float(y), float(w), float(h)] for x, y, w, h, _ in allf]
    scores = [float(sc) for sc in allf[:, 4]]
    keep = cv2.dnn.NMSBoxes(boxes, scores, 0.0, NMS_IOU)
    keep = np.array(keep).reshape(-1).tolist() if len(keep) else []
    # убираем рамки, почти целиком лежащие внутри другой (одно лицо на разных масштабах)
    keep.sort(key=lambda i: -boxes[i][2] * boxes[i][3])
    res: list[tuple[int, int, int, int, float]] = []
    kept_boxes: list[list[float]] = []
    for i in keep:
        x, y, w, h = boxes[i]
        if w < 4 or h < 4:
            continue
        if max(w, h) >= LARGE_FACE * min(H, W) and scores[i] < max(LARGE_FACE_SCORE, score_threshold):
            continue
        inside = False
        for X, Y, Wb, Hb in kept_boxes:
            ix = max(0.0, min(x + w, X + Wb) - max(x, X))
            iy = max(0.0, min(y + h, Y + Hb) - max(y, Y))
            if ix * iy > 0.6 * w * h:
                inside = True
                break
        if inside:
            continue
        kept_boxes.append([x, y, w, h])
        x0, y0 = max(0, int(round(x))), max(0, int(round(y)))
        x1, y1 = min(W, int(round(x + w))), min(H, int(round(y + h)))
        if x1 - x0 >= 2 and y1 - y0 >= 2:
            res.append((x0, y0, x1 - x0, y1 - y0, round(scores[i], 3)))
    res.sort(key=lambda b: (b[1], b[0]))
    return res


def blur(rgb: np.ndarray, boxes, mode: str = "blur") -> np.ndarray:
    """Скрывает лица: овал с мягким краем вокруг каждой рамки (запас PAD), так что лицо не узнать.

    mode="blur" — сильное размытие (σ ≈ размер лица / 3.5) поверх крупной пикселизации;
    mode="pixelate" — крупные квадраты (~4 по ширине лица, не мельче 4 px). Возвращает новую картинку.
    """
    if mode not in ("blur", "pixelate"):
        raise ValueError("mode: blur | pixelate")
    out = np.ascontiguousarray(rgb[..., :3]).copy()
    H, W = out.shape[:2]
    for b in boxes or []:
        x, y, w, h = (float(v) for v in b[:4])
        if w <= 1 or h <= 1:
            continue
        cx, cy = x + w / 2, y + h / 2
        # полуоси овала: рамка лица целиком внутри + запас PAD с каждой стороны
        ax = 0.5 * w * (1 + 2 * PAD) * 1.12
        ay = 0.5 * h * (1 + 2 * PAD) * 1.12
        feather = max(1.5, FEATHER * max(w, h))
        x0, x1 = int(max(0, cx - ax - 2 * feather)), int(min(W, cx + ax + 2 * feather + 1))
        y0, y1 = int(max(0, cy - ay - 2 * feather)), int(min(H, cy + ay + 2 * feather + 1))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        roi = out[y0:y1, x0:x1]
        rh, rw = roi.shape[:2]
        size = max(w, h)
        if mode == "pixelate":
            n = 4.0  # квадратов по ширине лица (при 7 и больше лицо ещё угадывается)
            bs = max(4.0, size / n)
            small = cv2.resize(roi, (max(1, int(rw / bs)), max(1, int(rh / bs))), interpolation=cv2.INTER_AREA)
            hidden = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
        else:
            # пикселизация + сильное размытие; размываем уменьшенную копию (на 12 Мп это в сотни раз
            # быстрее, чем гаусс с σ≈100 px по полному размеру), потом плавно увеличиваем
            bs = max(3.0, size / 5.0)
            small = cv2.resize(roi, (max(1, int(rw / bs)), max(1, int(rh / bs))), interpolation=cv2.INTER_AREA)
            sigma = max(3.0, size / 3.5)
            small = cv2.GaussianBlur(small, (0, 0), max(0.8, sigma / bs), borderType=cv2.BORDER_REPLICATE)
            hidden = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_CUBIC)
        # мягкая маска-овал: строим в уменьшенном виде (не крупнее ~128 px) и растягиваем
        ms = min(1.0, 128.0 / max(rw, rh))
        mw, mh = max(2, int(round(rw * ms))), max(2, int(round(rh * ms)))
        mask = np.zeros((mh, mw), np.float32)
        cv2.ellipse(mask, (int(round((cx - x0) * ms)), int(round((cy - y0) * ms))),
                    (max(1, int(round(ax * ms))), max(1, int(round(ay * ms)))), 0, 0, 360, 1.0, -1, cv2.LINE_AA)
        mask = cv2.GaussianBlur(mask, (0, 0), max(0.5, feather * ms))
        if ms < 1.0:
            mask = cv2.resize(mask, (rw, rh), interpolation=cv2.INTER_LINEAR)
        # внутри исходной рамки лица — строго 1 (никаких просветов от растушёвки)
        fx0, fy0 = int(max(0, x - x0)), int(max(0, y - y0))
        fx1, fy1 = int(min(rw, x + w - x0)), int(min(rh, y + h - y0))
        inner = np.zeros_like(mask)
        cv2.ellipse(inner, (int(round(cx - x0)), int(round(cy - y0))),
                    (max(1, int(round(ax - feather))), max(1, int(round(ay - feather)))), 0, 0, 360, 1.0, -1)
        inner[fy0:fy1, fx0:fx1] = 1.0
        mask = np.maximum(mask, inner)[..., None]
        out[y0:y1, x0:x1] = (roi * (1 - mask) + hidden * mask + 0.5).astype(np.uint8)
    return out
