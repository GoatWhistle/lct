"""Эмбеддинги для матчинга вопроса оператора к чек-листу.

Не regex: «на каком этаже?», «этаж какой?», «а этаж» пришлось бы перечислять
руками для каждого факта каждого сценария, а сценарии пишут методист и генератор
(docs/arch/STACK.md). Модель — multilingual-e5-small в ONNX: русский, CPU,
около 10 мс, и не отнимает процессор у распознавания речи.
"""

from pathlib import Path
from typing import Protocol

import numpy as np


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> np.ndarray:
        """Матрица нормированных векторов, строка на текст."""
        ...


def normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.clip(norms, 1e-12, None)


class E5Embedder:
    """multilingual-e5-small, квантованный ONNX.

    Для симметричной задачи «вопрос ↔ вопрос» e5 требует префикс `query: `
    с обеих сторон — без него близость фраз заметно проседает.
    """

    PREFIX = "query: "
    MAX_TOKENS = 128  # реплика оператора — одна-две фразы, длиннее не бывает

    def __init__(self, model_dir: Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_path = model_dir / "model_quantized.onnx"
        tokenizer_path = model_dir / "tokenizer.json"
        if not model_path.exists() or not tokenizer_path.exists():
            raise FileNotFoundError(
                f"нет модели эмбеддингов в {model_dir} — запусти `make models`"
            )

        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(max_length=self.MAX_TOKENS)
        self._tokenizer.enable_padding()

        options = ort.SessionOptions()
        # Два потока: модель делит процессор с распознаванием и синтезом речи.
        options.intra_op_num_threads = 2
        self._session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._inputs = {item.name for item in self._session.get_inputs()}

    def embed(self, texts: list[str]) -> np.ndarray:
        encoded = self._tokenizer.encode_batch([self.PREFIX + text for text in texts])
        input_ids = np.array([item.ids for item in encoded], dtype=np.int64)
        attention = np.array([item.attention_mask for item in encoded], dtype=np.int64)

        feed = {"input_ids": input_ids, "attention_mask": attention}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(input_ids)

        hidden = self._session.run(None, feed)[0]
        # Среднее по токенам без паддинга — так e5 обучалась.
        mask = attention[..., None].astype(hidden.dtype)
        pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
        return normalize(pooled)
