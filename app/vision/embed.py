"""Эмбеддинги фото и русских текстов в одном пространстве (CLIP ViT-B/32) на onnxruntime, без torch.

Модели (скачиваются один раз в `models_dir`, по умолчанию ``~/.cache/photo-report/models``,
переопределяется переменной окружения ``PHOTO_REPORT_MODELS``):

* картинки — ``Xenova/clip-vit-base-patch32``: ``onnx/vision_model_quantized.onnx`` (~89 МБ,
  variant="full" — ``onnx/vision_model.onnx``, ~350 МБ). Вход ``pixel_values`` (N,3,224,224) float32,
  выход ``image_embeds`` (N,512) — уже после проекции CLIP;
* тексты — ``sentence-transformers/clip-ViT-B-32-multilingual-v1`` (DistilBERT, 50+ языков, обучен
  повторять текстовые эмбеддинги OpenAI CLIP ViT-B/32): ``onnx/model_quint8_avx2.onnx`` на x86,
  ``onnx/model_qint8_arm64.onnx`` на Apple Silicon/ARM (~135 МБ), variant="full" — ``onnx/model.onnx``.
  Входы ``input_ids``/``attention_mask`` (int64), выход ``last_hidden_state`` (N,T,768);
  дальше mean pooling по attention mask и линейный слой 768→512 без смещения из
  ``2_Dense/model.safetensors`` (читаем safetensors сами).

Все эмбеддинги возвращаются L2-нормированными, float32, размерность 512.
"""
from __future__ import annotations

import json
import os
import platform
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np

DIM = 512

IMAGE_REPO = "Xenova/clip-vit-base-patch32"
TEXT_REPO = "sentence-transformers/clip-ViT-B-32-multilingual-v1"
HF_URL = "{endpoint}/{repo}/resolve/main/{file}"
# Откуда качать модели: своё зеркало (HF_ENDPOINT), Hugging Face, затем зеркало hf-mirror.com —
# huggingface.co в части сетей России и Китая недоступен или очень медленный. Попытки чередуются, докачка общая.
HF_ENDPOINTS = tuple(dict.fromkeys(e.rstrip("/") for e in (
    os.environ.get("HF_ENDPOINT", ""), "https://huggingface.co", "https://hf-mirror.com") if e))

# Препроцессинг CLIP (preprocessor_config.json модели): короткая сторона 224 (bicubic),
# центральный кроп 224×224, деление на 255, нормализация.
CLIP_SIZE = 224
CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

TEXT_MAX_TOKENS = 128  # sentence_bert_config.json: max_seq_length
# Квантованные модели квантуют активации динамически, по всему пакету сразу: эмбеддинг фото в пакете
# из 16 отличается от того же фото поодиночке (косинус 0.977–0.993), у текстов влияет и паддинг.
# Чтобы эмбеддинг зависел только от самого фото/текста, гоняем по одному: на CPU это почти не
# медленнее (16 фото: 0.66 с пакетом против 0.75 с по одному; текст — ~6 мс).
IMAGE_BATCH = 1
TEXT_BATCH = 1

# Примерные размеры файлов (байты) — только для оценки прогресса до начала скачивания.
_SIZE_HINTS = {
    "onnx/vision_model_quantized.onnx": 89_117_001,
    "onnx/vision_model.onnx": 351_685_709,
    "onnx/model_quint8_avx2.onnx": 135_377_779,
    "onnx/model_qint8_arm64.onnx": 135_336_307,
    "onnx/model.onnx": 539_049_415,
    "2_Dense/model.safetensors": 1_572_984,
    "tokenizer.json": 1_961_847,
}


class ModelUnavailableError(RuntimeError):
    """Модели распознавания нет на диске и скачать её не удалось."""


def default_models_dir() -> Path:
    env = os.environ.get("PHOTO_REPORT_MODELS")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".cache" / "photo-report" / "models"


def _is_arm() -> bool:
    return platform.machine().lower() in ("arm64", "aarch64", "armv8", "armv8l")


def _text_onnx_file(variant: str) -> str:
    if variant == "full":
        return "onnx/model.onnx"
    return "onnx/model_qint8_arm64.onnx" if _is_arm() else "onnx/model_quint8_avx2.onnx"


def _image_onnx_file(variant: str) -> str:
    return "onnx/vision_model.onnx" if variant == "full" else "onnx/vision_model_quantized.onnx"


def read_safetensors(path: str | os.PathLike) -> dict[str, np.ndarray]:
    """Минимальный читатель safetensors: 8 байт (uint64 LE) длины заголовка + JSON + сырые данные."""
    dtypes = {
        "F32": np.float32, "F16": np.float16, "F64": np.float64,
        "I64": np.int64, "I32": np.int32, "I16": np.int16, "I8": np.int8, "U8": np.uint8,
    }
    with open(path, "rb") as f:
        raw = f.read()
    (hlen,) = struct.unpack("<Q", raw[:8])
    header = json.loads(raw[8:8 + hlen].decode("utf-8"))
    base = 8 + hlen
    out: dict[str, np.ndarray] = {}
    for name, info in header.items():
        if name == "__metadata__":
            continue
        start, end = info["data_offsets"]
        buf = raw[base + start: base + end]
        dt = info["dtype"]
        if dt == "BF16":  # bfloat16 -> float32: старшие 16 бит float32
            u16 = np.frombuffer(buf, dtype="<u2").astype(np.uint32) << 16
            arr = u16.view(np.float32)
        else:
            arr = np.frombuffer(buf, dtype=np.dtype(dtypes[dt]).newbyteorder("<"))
        out[name] = arr.reshape(info["shape"]).astype(np.float32 if arr.dtype.kind == "f" else arr.dtype)
    return out


def _l2norm(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.maximum(n, 1e-12)


def preprocess_clip(rgb: np.ndarray) -> np.ndarray:
    """RGB uint8 HxWx3 -> (3,224,224) float32 как у CLIPImageProcessor."""
    from PIL import Image

    if rgb.ndim == 2:
        rgb = np.stack([rgb] * 3, axis=-1)
    rgb = np.ascontiguousarray(rgb[..., :3])
    h, w = rgb.shape[:2]
    scale = CLIP_SIZE / min(h, w)
    nw, nh = max(CLIP_SIZE, round(w * scale)), max(CLIP_SIZE, round(h * scale))
    img = Image.fromarray(rgb)
    # reducing_gap ускоряет уменьшение больших фото, на результат почти не влияет
    img = img.resize((nw, nh), Image.BICUBIC, reducing_gap=3.0)
    left, top = (nw - CLIP_SIZE) // 2, (nh - CLIP_SIZE) // 2
    img = img.crop((left, top, left + CLIP_SIZE, top + CLIP_SIZE))
    x = np.asarray(img, dtype=np.float32) / 255.0
    x = (x - CLIP_MEAN) / CLIP_STD
    return x.transpose(2, 0, 1)


class Embedder:
    """CLIP-эмбеддинги фото и русских текстов. Потокобезопасен; сессии ONNX создаются лениво.

    >>> emb = Embedder()
    >>> emb.ensure(lambda p: print(f"{p:.0%}"))     # скачать модели (один раз)
    >>> emb.encode_images([rgb1, rgb2]).shape      # (2, 512)
    >>> emb.encode_texts(["Монтаж сцены 6×4 м"])   # (1, 512)
    """

    def __init__(self, models_dir: str | os.PathLike | None = None, variant: str = "quantized"):
        if variant not in ("quantized", "full"):
            raise ValueError("variant: 'quantized' или 'full'")
        self.models_dir = Path(models_dir).expanduser() if models_dir else default_models_dir()
        self.variant = variant
        self.image_file = _image_onnx_file(variant)
        self.text_file = _text_onnx_file(variant)
        self._files: list[tuple[str, str]] = [
            (IMAGE_REPO, self.image_file),
            (TEXT_REPO, self.text_file),
            (TEXT_REPO, "tokenizer.json"),
            (TEXT_REPO, "2_Dense/model.safetensors"),
        ]
        self._lock = threading.RLock()          # создание сессий, статус
        self._dl_lock = threading.Lock()        # скачивание идёт в одном потоке, остальные ждут его
        self._img_run_lock = threading.Lock()   # один прогон картинок за раз (память/потоки CPU)
        self._txt_run_lock = threading.Lock()
        self._downloading = False
        self._progress = 0.0
        self._error = ""
        self._img_sess = None
        self._txt_sess = None
        self._tokenizer = None
        self._dense: np.ndarray | None = None   # (512, 768)
        self.io_info: dict = {}                 # реальные входы/выходы моделей (для отладки)

    # ---------- файлы ----------
    def _local(self, repo: str, file: str) -> Path:
        return self.models_dir / repo.replace("/", "__") / file

    def _missing(self) -> list[tuple[str, str]]:
        return [(r, f) for r, f in self._files if not self._local(r, f).is_file()]

    def total_size(self) -> int:
        """Ожидаемый размер всех файлов модели, байт (по подсказкам, либо по факту на диске)."""
        total = 0
        for r, f in self._files:
            p = self._local(r, f)
            total += p.stat().st_size if p.is_file() else _SIZE_HINTS.get(f, 0)
        return total

    def status(self) -> dict:
        with self._lock:
            missing = self._missing()
            ready = not missing
            return {
                "ready": ready,
                "downloading": self._downloading,
                "progress": 1.0 if ready else round(self._progress, 4),
                "error": self._error,
                "size_mb": round(self.total_size() / 1e6, 1),
                "models_dir": str(self.models_dir),
                "variant": self.variant,
                "missing": [f"{r}/{f}" for r, f in missing],
            }

    def ensure(self, progress_cb: Callable[[float], None] | None = None) -> None:
        """Скачивает недостающие файлы (с докачкой). progress_cb(доля 0..1) вызывается по ходу.

        Бросает ModelUnavailableError, если скачать не удалось (текст ошибки — и в status()["error"]).
        """
        with self._dl_lock:
            with self._lock:
                missing = self._missing()
                if missing:
                    self._downloading = True
                    self._error = ""
            if missing:
                try:
                    self._download_all(missing, progress_cb)
                except Exception as e:  # noqa: BLE001 — ошибку отдаём наружу понятным текстом
                    msg = f"Не удалось скачать модель распознавания: {e}"
                    with self._lock:
                        self._error = msg
                    raise ModelUnavailableError(msg) from e
                finally:
                    with self._lock:
                        self._downloading = False
            with self._lock:
                self._progress = 1.0
        if progress_cb:
            try:
                progress_cb(1.0)
            except Exception:  # noqa: BLE001
                pass

    def _download_all(self, missing, progress_cb) -> None:
        import requests

        verify = os.environ.get("REQUESTS_CA_BUNDLE", True)
        sizes = {f: _SIZE_HINTS.get(f, 1) for _, f in missing}
        total = max(1, sum(sizes.values()))
        done_before = 0
        last_cb = [0.0]

        def report(done_file: int) -> None:
            p = min(0.999, (done_before + done_file) / total)
            with self._lock:
                self._progress = p
            now = time.monotonic()
            if progress_cb and (now - last_cb[0] > 0.2):
                last_cb[0] = now
                try:
                    progress_cb(p)
                except Exception:  # noqa: BLE001 — колбэк не должен ронять скачивание
                    pass

        with requests.Session() as s:
            s.headers["User-Agent"] = "photo-report/1.0"
            for repo, file in missing:
                dst = self._local(repo, file)
                expected = self._download_one(s, verify, repo, file, dst, report)
                sizes[file] = expected
                done_before += expected

    def _download_one(self, s, verify, repo: str, file: str, dst: Path, report) -> int:
        """Скачивает один файл в dst.part с докачкой (Range), проверяет размер, переименовывает."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        part = dst.with_name(dst.name + ".part")
        last_err: Exception | None = None
        for attempt in range(3 * len(HF_ENDPOINTS)):
            url = HF_URL.format(endpoint=HF_ENDPOINTS[attempt % len(HF_ENDPOINTS)], repo=repo, file=file)
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                with s.get(url, headers=headers, stream=True, timeout=(15, 60), verify=verify) as r:
                    if r.status_code == 416:  # диапазон за концом файла: .part полный или испорчен
                        if have != _SIZE_HINTS.get(file):
                            part.unlink(missing_ok=True)
                            raise IOError(f"{file}: недокачанный файл не совпал с сервером, качаем заново")
                        total = have
                    else:
                        r.raise_for_status()
                        if r.status_code == 206:
                            rng = r.headers.get("Content-Range", "")  # bytes a-b/total
                            total = int(rng.rsplit("/", 1)[-1]) if "/" in rng else have + int(r.headers.get("Content-Length", 0))
                            mode = "ab"
                        else:  # сервер отдал файл целиком — начинаем заново
                            have = 0
                            total = int(r.headers.get("Content-Length", 0)) or None
                            mode = "wb"
                        with open(part, mode) as f:
                            got = have
                            for chunk in r.iter_content(chunk_size=1 << 20):
                                if not chunk:
                                    continue
                                f.write(chunk)
                                got += len(chunk)
                                report(got)
                        total = total or got
                size = part.stat().st_size
                if size != total:
                    if size > total:
                        part.unlink(missing_ok=True)
                    raise IOError(f"{file}: скачано {size} из {total} байт")
                os.replace(part, dst)
                return size
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(min(8, 1 + attempt))
        raise IOError(f"{repo}/{file}: {last_err}")

    # ---------- сессии ----------
    def _require(self) -> None:
        missing = self._missing()
        if missing:
            names = ", ".join(f for _, f in missing)
            raise ModelUnavailableError(
                f"Модель распознавания не скачана ({names}). Вызовите ensure() или проверьте интернет; "
                f"папка моделей: {self.models_dir}")

    def _session(self, path: Path):
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.log_severity_level = 3
        return ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])

    def _io(self, sess) -> dict:
        return {
            "inputs": [(i.name, i.type, i.shape) for i in sess.get_inputs()],
            "outputs": [(o.name, o.type, o.shape) for o in sess.get_outputs()],
        }

    def _image_session(self):
        with self._lock:
            if self._img_sess is None:
                self._require()
                sess = self._session(self._local(IMAGE_REPO, self.image_file))
                self.io_info["image"] = self._io(sess)
                outs = [o.name for o in sess.get_outputs()]
                self._img_out = "image_embeds" if "image_embeds" in outs else outs[0]
                self._img_in = sess.get_inputs()[0].name
                self._img_sess = sess
            return self._img_sess

    def _text_session(self):
        with self._lock:
            if self._txt_sess is None:
                self._require()
                from tokenizers import Tokenizer

                tok = Tokenizer.from_file(str(self._local(TEXT_REPO, "tokenizer.json")))
                tok.enable_truncation(max_length=TEXT_MAX_TOKENS)
                tok.enable_padding(pad_id=0, pad_token="[PAD]")
                dense = read_safetensors(self._local(TEXT_REPO, "2_Dense/model.safetensors"))
                w = dense.get("linear.weight")
                if w is None:
                    w = next(v for k, v in dense.items() if k.endswith("weight"))
                if w.shape != (DIM, 768):
                    raise ModelUnavailableError(f"Неожиданная форма Dense: {w.shape}")
                self._dense_bias = dense.get("linear.bias")
                sess = self._session(self._local(TEXT_REPO, self.text_file))
                self.io_info["text"] = self._io(sess)
                self._txt_inputs = [i.name for i in sess.get_inputs()]
                outs = [o.name for o in sess.get_outputs()]
                self._txt_out = "last_hidden_state" if "last_hidden_state" in outs else outs[0]
                self._tokenizer, self._dense, self._txt_sess = tok, w.astype(np.float32), sess
            return self._txt_sess

    # ---------- кодирование ----------
    def encode_images(self, images: Sequence[np.ndarray]) -> np.ndarray:
        """Список RGB uint8 -> (N,512) float32, L2-нормировано (~90 мс на фото на CPU).

        Результат для фото не зависит от того, с какими ещё фото оно передано (см. IMAGE_BATCH)."""
        if len(images) == 0:
            return np.zeros((0, DIM), np.float32)
        sess = self._image_session()
        out = []
        for i in range(0, len(images), IMAGE_BATCH):
            batch = np.stack([preprocess_clip(im) for im in images[i:i + IMAGE_BATCH]]).astype(np.float32)
            with self._img_run_lock:
                (emb,) = sess.run([self._img_out], {self._img_in: batch})
            if emb.ndim == 3:  # на всякий случай, если выход не pooled
                emb = emb[:, 0]
            out.append(emb)
        return _l2norm(np.concatenate(out))

    def encode_texts(self, texts: Iterable[str]) -> np.ndarray:
        """Список строк (любой язык, в т.ч. русский) -> (M,512) float32, L2-нормировано."""
        texts = [str(t) if t else " " for t in texts]
        if not texts:
            return np.zeros((0, DIM), np.float32)
        sess = self._text_session()
        out = []
        for i in range(0, len(texts), TEXT_BATCH):
            enc = self._tokenizer.encode_batch(texts[i:i + TEXT_BATCH])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._txt_inputs:
                feeds["token_type_ids"] = np.zeros_like(ids)
            feeds = {k: v for k, v in feeds.items() if k in self._txt_inputs}
            with self._txt_run_lock:
                (hidden,) = sess.run([self._txt_out], feeds)
            m = mask[..., None].astype(np.float32)
            pooled = (hidden * m).sum(1) / np.maximum(m.sum(1), 1e-9)   # mean pooling
            proj = pooled @ self._dense.T                                 # Dense 768 -> 512
            if self._dense_bias is not None:
                proj = proj + self._dense_bias
            out.append(proj)
        return _l2norm(np.concatenate(out))
