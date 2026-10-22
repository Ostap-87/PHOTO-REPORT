"""Тесты app/vision без сети: синтетические картинки и эмбеддинги.

Тест настоящей модели эмбеддингов пропускается, если модели не скачаны (Embedder().ensure()).
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.vision import classify, dedup, enhance, faces, quality  # noqa: E402
from app.vision import io as vio  # noqa: E402
from app.vision import embed  # noqa: E402


# ---------------------------------------------------------------- синтетические «фото»
def scene(h: int = 480, w: int = 640, seed: int = 0) -> np.ndarray:
    """Псевдо-фото: градиентный фон, цветные фигуры с резкими краями, мелкая текстура."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack([60 + 120 * x / w, 70 + 100 * y / h, 150 - 60 * x / w], -1)
    for _ in range(14):
        c = rng.integers(20, 235, 3).tolist()
        x0, y0 = int(rng.integers(0, w - 60)), int(rng.integers(0, h - 60))
        if rng.random() < 0.5:
            cv2.rectangle(img, (x0, y0), (x0 + int(rng.integers(30, 160)), y0 + int(rng.integers(30, 120))), c, -1)
        else:
            cv2.circle(img, (x0 + 30, y0 + 30), int(rng.integers(12, 60)), c, -1)
    for i in range(0, w, 16):  # «текстура» — тонкие линии
        cv2.line(img, (i, 0), (i, h), (200, 200, 200), 1)
    img += rng.normal(0, 2.0, img.shape).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.uint8)


def to_linear_gain(rgb: np.ndarray, k) -> np.ndarray:
    lin = (rgb / 255.0) ** 2.2 * np.asarray(k, np.float32)
    return (np.clip(lin, 0, 1) ** (1 / 2.2) * 255 + 0.5).astype(np.uint8)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 99.0 if mse == 0 else 10 * np.log10(255 ** 2 / mse)


def _diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(int) - b.astype(int)).mean())


def make_exif(orientation: int = 6) -> bytes:
    ex = Image.Exif()
    ex[0x0112] = orientation
    ex[0x010F] = "Apple"
    ex[0x0110] = "iPhone 15"
    ex[0x0132] = "2026:10:21 14:03:11"
    ex.get_ifd(0x8769)[0x9003] = "2026:10:21 14:03:11"
    gps = ex.get_ifd(0x8825)
    gps[1], gps[2] = "N", (55.0, 45.0, 0.0)
    gps[3], gps[4] = "W", (37.0, 37.0, 12.0)
    return ex.tobytes()


# ---------------------------------------------------------------- io
def test_load_jpeg_exif_rotation_and_meta(tmp_path):
    rgb = scene(300, 400)
    p = tmp_path / "a.jpg"
    Image.fromarray(rgb).save(p, exif=make_exif(6), quality=95)
    out, exif_bytes, meta = vio.load_image(p)
    assert out.dtype == np.uint8 and out.shape == (400, 300, 3)       # повёрнуто на 90°
    assert exif_bytes and meta["orientation"] == 6
    assert meta["datetime"] == "2026-10-21 14:03:11"
    assert meta["camera"] == "Apple iPhone 15"
    assert meta["gps"] == pytest.approx([55.75, -(37 + 37 / 60 + 12 / 3600)], abs=1e-5)
    assert (meta["w"], meta["h"]) == (300, 400)
    # поворот по часовой стрелке (Orientation=6), а не против
    assert _diff(out, np.rot90(rgb, k=-1)) < 8 < _diff(out, np.rot90(rgb, k=1))


def test_save_jpeg_keeps_exif_resets_orientation(tmp_path):
    rgb = scene(300, 400)
    src = tmp_path / "a.jpg"
    Image.fromarray(rgb).save(src, exif=make_exif(6), quality=95)
    out, exif_bytes, meta = vio.load_image(src)
    dst = tmp_path / "sub" / "w.jpg"
    vio.save_jpeg(out, dst, exif_bytes=exif_bytes, quality=92)
    again, ex2, meta2 = vio.load_image(dst)
    assert again.shape == out.shape                                   # второй раз не поворачивается
    assert meta2["orientation"] == 1
    assert meta2["datetime"] == meta["datetime"] and meta2["gps"] == meta["gps"]
    assert meta2["camera"] == "Apple iPhone 15"
    assert not list(dst.parent.glob(".tmp-*"))                        # временных файлов не осталось
    vio.save_jpeg(out, tmp_path / "noexif.jpg")                       # без EXIF тоже работает
    assert vio.load_image(tmp_path / "noexif.jpg")[1] is None


def test_load_heic(tmp_path):
    pytest.importorskip("pillow_heif")
    rgb = scene(240, 320)
    p = tmp_path / "a.heic"
    Image.fromarray(rgb).save(p, exif=make_exif(6), quality=90)
    out, exif_bytes, meta = vio.load_image(p)
    assert out.shape == (320, 240, 3)
    assert meta["format"] in ("HEIF", "HEIC")
    assert meta["datetime"] == "2026-10-21 14:03:11" and meta["camera"] == "Apple iPhone 15"
    assert meta["orientation"] == 6
    assert _diff(out, np.rot90(rgb, k=-1)) < 12 < _diff(out, np.rot90(rgb, k=1))   # HEIC с потерями


def test_load_png_rgba_16bit_webp_tiff(tmp_path):
    rgba = np.zeros((50, 60, 4), np.uint8)
    rgba[..., 0] = 255
    rgba[:25, :, 3] = 255                                             # нижняя половина прозрачная
    Image.fromarray(rgba, "RGBA").save(tmp_path / "a.png")
    out, ex, meta = vio.load_image(tmp_path / "a.png")
    assert out.shape == (50, 60, 3) and ex is None and meta["gps"] is None
    assert tuple(out[10, 10]) == (255, 0, 0) and tuple(out[40, 10]) == (255, 255, 255)

    g16 = (np.linspace(0, 65535, 64 * 64).reshape(64, 64)).astype(np.uint16)
    Image.fromarray(g16).save(tmp_path / "g16.png")
    out16 = vio.load_image(tmp_path / "g16.png")[0]
    assert out16.dtype == np.uint8 and out16.shape == (64, 64, 3) and out16.max() >= 250

    rgb = scene(120, 160)
    for ext in ("webp", "tiff"):
        Image.fromarray(rgb).save(tmp_path / f"a.{ext}")
        o = vio.load_image(tmp_path / f"a.{ext}")[0]
        assert o.shape == (120, 160, 3)


def test_thumbnail():
    rgb = scene(480, 640)
    t = vio.thumbnail(rgb, 320)
    assert t.shape == (240, 320, 3)
    small = vio.thumbnail(rgb, 1000)
    assert small.shape == rgb.shape and small is not rgb


# ---------------------------------------------------------------- quality
def test_quality_good_photo_has_no_flags():
    q = quality.analyze(scene())
    assert q["flags"] == [], q
    assert not q["needs_enhance"] and not q["reshoot"]
    json.dumps(q)  # всё сериализуется в JSON
    for k in ("brightness", "contrast", "blur", "noise", "sharpness", "clip_high", "clip_low", "cast"):
        assert isinstance(q[k], float)


@pytest.mark.parametrize("name,fn,flag", [
    ("dark", lambda x: to_linear_gain(x, 0.06), "dark"),
    ("over", lambda x: to_linear_gain(x, 4.0), "overexposed"),
    ("lowc", lambda x: (x * 0.4 + 80).astype(np.uint8), "low_contrast"),
    ("blur", lambda x: cv2.GaussianBlur(x, (0, 0), 2.0), "blurry"),
    ("cast", lambda x: to_linear_gain(x, (1.35, 1.0, 0.65)), "color_cast"),
])
def test_quality_detects_defects(name, fn, flag):
    q = quality.analyze(fn(scene()))
    assert flag in q["flags"], (name, q)
    assert q["needs_enhance"]


def test_quality_very_blurry_means_reshoot():
    q = quality.analyze(cv2.GaussianBlur(scene(), (0, 0), 6.0))
    assert "very_blurry" in q["flags"] and q["reshoot"]


def test_quality_black_frame_reshoot():
    q = quality.analyze(np.full((200, 300, 3), 2, np.uint8))
    assert q["reshoot"] and "dark" in q["flags"]


def test_quality_large_image_uses_small_copy():
    big = cv2.resize(scene(), (3000, 2250), interpolation=cv2.INTER_CUBIC)
    q = quality.analyze(big)
    assert "dark" not in q["flags"] and "overexposed" not in q["flags"]


# ---------------------------------------------------------------- enhance
def test_enhance_auto_keeps_good_photo_and_off():
    x = scene()
    q = quality.analyze(x)
    y, applied = enhance.enhance(x, q, "auto")
    assert applied == [] and np.array_equal(x, y)
    y, applied = enhance.enhance(x, q, "off")
    assert applied == [] and np.array_equal(x, y)
    with pytest.raises(ValueError):
        enhance.enhance(x, q, "magic")


def test_enhance_all_is_gentle_on_good_photo():
    x = scene()
    y, applied = enhance.enhance(x, None, "all")
    assert applied and y.shape == x.shape and y.dtype == np.uint8
    assert psnr(x, y) > 27


def test_enhance_fixes_dark_photo():
    x = scene()
    d = to_linear_gain(x, 0.06)
    q = quality.analyze(d)
    y, applied = enhance.enhance(d, q, "auto")
    assert "exposure" in applied
    assert psnr(y, x) > psnr(d, x) + 5
    assert "dark" not in quality.analyze(y)["flags"]


def test_enhance_fixes_low_contrast_and_cast():
    x = scene()
    lc = (x * 0.4 + 80).astype(np.uint8)
    y, applied = enhance.enhance(lc, quality.analyze(lc), "auto")
    assert "levels" in applied and quality.analyze(y)["contrast"] > quality.analyze(lc)["contrast"] * 1.8
    c = to_linear_gain(x, (1.35, 1.0, 0.65))
    qc = quality.analyze(c)
    y, applied = enhance.enhance(c, qc, "auto")
    assert "white_balance" in applied
    assert quality.analyze(y)["cast"] < qc["cast"] * 0.6


def test_enhance_sharpens_blurry():
    x = scene()
    b = cv2.GaussianBlur(x, (0, 0), 1.6)
    q = quality.analyze(b)
    assert "blurry" in q["flags"] and "very_blurry" not in q["flags"]
    y, applied = enhance.enhance(b, q, "auto")
    assert "sharpen" in applied
    assert quality.analyze(y)["sharpness"] > q["sharpness"] * 1.5


# ---------------------------------------------------------------- faces
def test_faces_detect_on_empty_and_noise():
    assert faces.detect(np.full((240, 320, 3), 128, np.uint8)) == []
    rng = np.random.default_rng(1)
    res = faces.detect(rng.integers(0, 255, (480, 640, 3), dtype=np.uint8))
    assert isinstance(res, list)
    for b in res:
        assert len(b) == 5


def test_faces_backend_is_yunet():
    assert faces.MODEL_PATH.is_file()
    assert faces.backend() == "yunet"


def test_faces_blur_hides_region_and_keeps_rest():
    x = scene(480, 640, seed=3)
    box = (200, 150, 120, 150, 0.9)
    for mode in ("blur", "pixelate"):
        y = faces.blur(x, [box], mode=mode)
        assert y.shape == x.shape and not np.shares_memory(x, y)
        inner = (slice(170, 280), slice(215, 305))
        # внутри рамки мелкие детали уничтожены
        lap_x = cv2.Laplacian(cv2.cvtColor(x[inner], cv2.COLOR_RGB2GRAY), cv2.CV_64F).var()
        lap_y = cv2.Laplacian(cv2.cvtColor(y[inner], cv2.COLOR_RGB2GRAY), cv2.CV_64F).var()
        assert lap_y < lap_x * 0.15, (mode, lap_x, lap_y)
        # далеко от рамки ничего не изменилось
        assert np.array_equal(x[:60, :120], y[:60, :120])
        assert np.array_equal(x[420:, 520:], y[420:, 520:])
    with pytest.raises(ValueError):
        faces.blur(x, [box], mode="emoji")
    assert np.array_equal(faces.blur(x, []), x)


def test_faces_blur_box_at_border():
    x = scene(200, 200)
    y = faces.blur(x, [(-10, -10, 60, 60, 0.8), (180, 180, 40, 40, 0.7)])
    assert y.shape == x.shape


def test_faces_haar_fallback(monkeypatch):
    monkeypatch.setattr(faces._local, "yunet", None, raising=False)
    monkeypatch.setattr(faces._local, "yunet_failed", True, raising=False)
    assert faces.backend() == "haar"
    assert faces.detect(np.full((240, 320, 3), 100, np.uint8)) == []


# ---------------------------------------------------------------- dedup
def test_dhash_near_duplicates_and_groups(tmp_path):
    a = scene(480, 640, seed=1)
    a_small = cv2.resize(a, (320, 240), interpolation=cv2.INTER_AREA)
    p = tmp_path / "a.jpg"
    Image.fromarray(a).save(p, quality=60)
    a_jpeg = np.asarray(Image.open(p).convert("RGB"))
    a_bright = np.clip(a.astype(int) + 12, 0, 255).astype(np.uint8)
    b = scene(480, 640, seed=7)
    c = np.ascontiguousarray(a[:, ::-1])
    hs = [dedup.dhash(z) for z in (a, a_small, a_jpeg, a_bright, b, c)]
    assert all(0 <= h < 2 ** 64 for h in hs)
    assert dedup.hamming(hs[0], hs[1]) <= 6 and dedup.hamming(hs[0], hs[2]) <= 6 and dedup.hamming(hs[0], hs[3]) <= 6
    assert dedup.hamming(hs[0], hs[4]) > 12 and dedup.hamming(hs[0], hs[5]) > 12
    assert dedup.groups(hs) == [[0, 1, 2, 3]]
    assert dedup.groups([hs[0]]) == [] and dedup.groups([]) == []


def test_dedup_groups_with_embeddings():
    hs = [0, (1 << 10) - 1, (1 << 40) - 1]            # расстояния 10 и 40 — по dHash не дубли
    e = np.array([[1, 0, 0], [0.99, 0.141, 0], [0.99, 0.141, 0]], np.float32)
    assert dedup.groups(hs) == []
    assert dedup.groups(hs, embs=e) == [[0, 1]]        # серия: близкий dHash + похожий смысл
    assert dedup.groups(hs, embs=e, min_cos=0.999) == []


def test_dedup_groups_transitive_and_best():
    hs = [0b0, 0b111, 0b111111, 1 << 40 | 0xFFFF]
    assert dedup.groups(hs, max_dist=3) == [[0, 1, 2]]
    assert dedup.best([0, 1, 2], [10.0, 50.0, 30.0, 99.0]) == 1         # по общему списку
    assert dedup.best([2, 3], [5.0, 7.0]) == 3                          # выровнено по группе
    assert dedup.best([1, 3], {1: 3.0, 3: 2.0}) == 1


# ---------------------------------------------------------------- classify
def test_clean_text_and_build_prompts():
    assert classify.clean_text("1.2. Расстановка банкетных столов с текстилем — 12 шт.") == \
        "Расстановка банкетных столов с текстилем"
    assert classify.clean_text("3) Монтаж сцены 6×4 м (1 компл.)") == "Монтаж сцены 6×4 м"
    assert classify.clean_text("Кабельные трассы в серверной, в количестве 4 шт") == "Кабельные трассы в серверной"
    assert classify.clean_text("п. 4 Покраска стен — 120 м²") == "Покраска стен — 120 м²"   # площадь не трогаем
    item = {"num": "2.1", "title": "2.1 Брендирование входной группы", "hint": "баннер над входом",
            "text": "Брендирование входной группы баннером 3×2 м. Монтаж и демонтаж."}
    pr = classify.build_prompts(item)
    assert pr[:3] == ["Фотография: брендирование входной группы", "фото: Брендирование входной группы",
                      "Снимок: брендирование входной группы"]
    assert "Фотография: баннер над входом" in pr and "Снимок: баннер над входом" in pr
    assert any("3×2 м" in p for p in pr) and not any("Монтаж и демонтаж" in p for p in pr)
    assert len(pr) == len({p.lower() for p in pr})
    assert classify.build_prompts({"num": "5"}) == ["пункт 5"]
    assert classify.build_prompts({"title": "LED-экран"})[0] == "Фотография: LED-экран"


def _unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def _synthetic(n_items=4, per=6, dim=64, noise=0.2, seed=0, bad=None):
    """Эмбеддинги «как у CLIP»: тексты похожи друг на друга (общая часть), фото тоже (общий сдвиг);
    правильный текст ближе к фото всего на ~0.05 косинуса, фото одного пункта похожи между собой."""
    rng = np.random.default_rng(seed)
    basis = np.linalg.qr(rng.normal(size=(dim, dim)))[0].T     # ортонормированные направления
    common_t, common_x = basis[0], basis[1]
    r = basis[2:2 + n_items]                                    # смысл пункта (текст и фото)
    v = basis[2 + n_items:2 + 2 * n_items]                      # «как выглядит» пункт (только фото)
    texts = {f"it{j}": _unit(0.9 * common_t + 0.44 * r[j] + 0.03 * rng.normal(size=(3, dim)))
             for j in range(n_items)}
    if bad is not None:   # неудачная формулировка пункта: общая «текстовая» часть есть, смысла нет
        texts[bad] = _unit(0.9 * common_t + 0.44 * basis[2 + 2 * n_items] + 0.03 * rng.normal(size=(3, dim)))
    photos, labels = [], []
    for j in range(n_items):
        for _ in range(per):
            x = 0.25 * common_t + 0.12 * r[j] + 0.7 * common_x + 0.45 * v[j] + noise * rng.normal(size=dim) / np.sqrt(dim)
            photos.append(_unit(x))
            labels.append(f"it{j}")
    return np.stack(photos), texts, labels


def test_classify_zero_shot_structure_and_accuracy():
    X, T, y = _synthetic()
    res = classify.classify(X, T, {}, {})
    assert len(res) == len(X)
    for r in res:
        assert set(r) == {"item_id", "confidence", "suggestions"}
        assert len(r["suggestions"]) == 3
        assert abs(sum(p for _, p in r["suggestions"]) - 1) < 1 or len(T) > 3
        assert r["item_id"] in (None, *T.keys())
        assert 0 <= r["confidence"] <= 1
    top1 = [r["suggestions"][0][0] for r in res]
    assert np.mean([a == b for a, b in zip(top1, y)]) >= 0.9
    assigned = [(r["item_id"], t) for r, t in zip(res, y) if r["item_id"]]
    assert assigned and all(a == t for a, t in assigned)


def test_classify_thresholds_send_to_unsorted():
    X, T, _ = _synthetic()
    res = classify.classify(X, T, {}, {"threshold": 1.01, "margin": 0.0})
    assert all(r["item_id"] is None for r in res)
    res = classify.classify(X, T, {}, {"threshold": 0.0, "margin": 1.01})
    assert all(r["item_id"] is None for r in res)
    res = classify.classify(X, T, {}, {"threshold": 0.0, "margin": 0.0})
    assert all(r["item_id"] is not None for r in res)


def test_classify_examples_fix_mistakes():
    # текст пункта it1 «не похож» на свои фото — без примеров они уходят не туда
    X, T_bad, y = _synthetic(seed=4, bad="it1")
    before = classify.classify(X, T_bad, {}, {})
    idx1 = [i for i, t in enumerate(y) if t == "it1"]
    acc_before = np.mean([before[i]["suggestions"][0][0] == "it1" for i in idx1])
    ex = {"it1": X[idx1[:2]]}
    after = classify.classify(X, T_bad, ex, {})
    acc_after = np.mean([after[i]["suggestions"][0][0] == "it1" for i in idx1[2:]])
    assert acc_after > acc_before and acc_after >= 0.75
    # примеры одного пункта не перетягивают фото других пунктов
    others = [i for i, t in enumerate(y) if t != "it1"]
    assert np.mean([after[i]["suggestions"][0][0] != "it1" for i in others]) >= 0.85


def test_classify_edge_cases():
    X, T, _ = _synthetic()
    assert classify.classify(np.zeros((0, X.shape[1])), T, {}, {}) == []
    res = classify.classify(X[:2], {}, {}, {})
    assert all(r["item_id"] is None and r["suggestions"] == [] for r in res)
    res = classify.classify(X[:1], {"only": T["it0"]}, {}, {})
    assert res[0]["confidence"] == pytest.approx(1.0) and res[0]["suggestions"] == [["only", 1.0]]
    # пункт только с примерами (без текста) тоже участвует
    res = classify.classify(X[:3], {"it0": T["it0"]}, {"x": X[3:6]}, {})
    assert {s[0] for s in res[0]["suggestions"]} == {"it0", "x"}


# ---------------------------------------------------------------- embed (без сети)
def test_read_safetensors(tmp_path):
    w = np.arange(12, dtype=np.float32).reshape(3, 4)
    b16 = np.array([1.5, -2.0], dtype=np.float16)
    header = {"linear.weight": {"dtype": "F32", "shape": [3, 4], "data_offsets": [0, 48]},
              "h": {"dtype": "F16", "shape": [2], "data_offsets": [48, 52]},
              "__metadata__": {"format": "pt"}}
    hb = json.dumps(header).encode()
    p = tmp_path / "m.safetensors"
    p.write_bytes(struct.pack("<Q", len(hb)) + hb + w.tobytes() + b16.tobytes())
    t = embed.read_safetensors(p)
    assert np.array_equal(t["linear.weight"], w) and t["h"].tolist() == [1.5, -2.0]


def test_preprocess_clip_shape():
    x = embed.preprocess_clip(scene(300, 500))
    assert x.shape == (3, 224, 224) and x.dtype == np.float32
    assert -3 < x.min() < 0 < x.max() < 3


def test_embedder_status_without_models(tmp_path, monkeypatch):
    e = embed.Embedder(tmp_path / "none")
    st = e.status()
    assert st["ready"] is False and st["downloading"] is False and 200 < st["size_mb"] < 260
    with pytest.raises(embed.ModelUnavailableError):
        e.encode_texts(["тест"])
    monkeypatch.setenv("PHOTO_REPORT_MODELS", str(tmp_path / "env"))
    assert embed.Embedder().models_dir == tmp_path / "env"


def test_embedder_text_file_by_arch(monkeypatch):
    monkeypatch.setattr(embed.platform, "machine", lambda: "arm64")
    assert embed.Embedder("/tmp/x").text_file == "onnx/model_qint8_arm64.onnx"
    monkeypatch.setattr(embed.platform, "machine", lambda: "x86_64")
    assert embed.Embedder("/tmp/x").text_file == "onnx/model_quint8_avx2.onnx"
    assert embed.Embedder("/tmp/x", variant="full").text_file == "onnx/model.onnx"


_EMB = embed.Embedder()


@pytest.mark.skipif(not _EMB.status()["ready"], reason="модели CLIP не скачаны (Embedder().ensure())")
def test_embedder_real_models():
    t = _EMB.encode_texts(["Расстановка банкетных столов с текстилем", "Кабельные трассы в серверной", ""])
    assert t.shape == (3, 512) and t.dtype == np.float32
    assert np.allclose(np.linalg.norm(t, axis=1), 1, atol=1e-4)
    imgs = [scene(300, 400, seed=s) for s in range(3)]
    v = _EMB.encode_images(imgs)
    assert v.shape == (3, 512) and np.allclose(np.linalg.norm(v, axis=1), 1, atol=1e-4)
    assert np.allclose(_EMB.encode_images(imgs[:1]), v[:1], atol=1e-5)    # детерминированно
    assert _EMB.io_info["image"]["outputs"][0][0] == "image_embeds"
    assert _EMB.io_info["text"]["outputs"][0][0] == "last_hidden_state"
    assert _EMB.encode_images([]).shape == (0, 512) and _EMB.encode_texts([]).shape == (0, 512)
