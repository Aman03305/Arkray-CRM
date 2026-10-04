"""Embedding providers (docs/rag-architecture.md#embeddings).

Embeddings are computed inside the deployment by a pinned open model,
BAAI/bge-small-en-v1.5 (MIT licence, 384 dimensions, ONNX on CPU): indexing sends no CRM
text to any third party, costs nothing per record, and keeps working when the internet or
a vendor does not. The model files are fetched once, verified against pinned SHA-256
digests (ai.model_files, run at image build) and loaded from disk; the runtime never
downloads anything.

Providers:
- `LocalEmbedder` ("local"): the model above. Documents are embedded as-is; queries get
  the model's retrieval instruction. CLS pooling, L2-normalised (the model's recipe).
- `HashingEmbedder` ("hashing"): a deterministic bag-of-words-and-trigrams vector for tests
  and offline development. Lexical, not semantic; production settings refuse it.

Any failure to embed raises `EmbeddingUnavailable`; callers degrade (indexing backs off in
the outbox, retrieval reports note search as unavailable). The CRM never waits on this.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from pathlib import Path
from typing import Any, Protocol

from django.conf import settings

from .models import EMBEDDING_DIMENSIONS

logger = logging.getLogger(__name__)

# bge-*-v1.5's instruction for short retrieval queries (documents get none).
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
MAX_TOKENS = 512  # the model's sequence limit; chunks are sized well below it
BATCH_SIZE = 16


class EmbeddingUnavailable(Exception):
    """The embedding model could not produce vectors (missing files, load or run failure)."""


class Embedder(Protocol):
    model_name: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _normalised(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


class HashingEmbedder:
    """Tests and offline development only. Words and character trigrams are hashed into the
    vector's dimensions (signed), then L2-normalised: texts sharing words are close."""

    model_name = "hashing-v1"
    _WORD = re.compile(r"\w+", re.UNICODE)

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMENSIONS
        words = self._WORD.findall(text.casefold())
        features = list(words)
        for word in words:
            padded = f"#{word}#"
            features.extend(padded[i : i + 3] for i in range(len(padded) - 2))
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        return _normalised(vector)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class LocalEmbedder:
    model_name = "bge-small-en-v1.5"

    def __init__(self, model_dir: Path, *, threads: int) -> None:
        self._model_dir = model_dir
        self._threads = threads
        self._session: Any = None
        self._tokenizer: Any = None
        self._lock = threading.Lock()

    def _load(self) -> None:
        # Imported lazily: web processes never load the model (questions and indexing run
        # in the ai workers), so they don't pay its memory or start-up time.
        import onnxruntime  # type: ignore[import-untyped]  # ships no type information
        from tokenizers import Tokenizer

        model_path = self._model_dir / "model.onnx"
        tokenizer_path = self._model_dir / "tokenizer.json"
        if not model_path.is_file() or not tokenizer_path.is_file():
            raise EmbeddingUnavailable(
                "Embedding model files are missing; run `manage.py ai_fetch_model`."
            )
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
        tokenizer.enable_truncation(max_length=MAX_TOKENS)
        tokenizer.enable_padding()
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = self._threads
        options.inter_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = tokenizer
        logger.info("embedding_model_loaded", extra={"model": self.model_name})

    def _run(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        with self._lock:  # one inference at a time per process; the session is reused
            if self._session is None:
                self._load()
            vectors: list[list[float]] = []
            for start in range(0, len(texts), BATCH_SIZE):
                encoded = self._tokenizer.encode_batch(texts[start : start + BATCH_SIZE])
                inputs = {
                    "input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
                    "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
                    "token_type_ids": np.array([e.type_ids for e in encoded], dtype=np.int64),
                }
                hidden = self._session.run(["last_hidden_state"], inputs)[0]
                cls = hidden[:, 0]
                cls = cls / np.linalg.norm(cls, axis=1, keepdims=True)
                vectors.extend(row.astype(float).tolist() for row in cls)
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            return self._run(texts)
        except EmbeddingUnavailable:
            raise
        except Exception as exc:
            raise EmbeddingUnavailable(type(exc).__name__) from exc

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([QUERY_INSTRUCTION + text])[0]


_embedders: dict[tuple[str, str], Embedder] = {}
_embedders_lock = threading.Lock()


def get_embedder() -> Embedder:
    """The configured provider, created once per process (the model loads on first use)."""
    provider = settings.AI_EMBEDDING_PROVIDER
    key = (provider, settings.AI_EMBEDDING_MODEL_DIR)
    with _embedders_lock:
        if key not in _embedders:
            if provider == "local":
                _embedders[key] = LocalEmbedder(
                    Path(settings.AI_EMBEDDING_MODEL_DIR), threads=settings.AI_EMBEDDING_THREADS
                )
            elif provider == "hashing":
                _embedders[key] = HashingEmbedder()
            else:
                raise EmbeddingUnavailable(f"Unknown embedding provider {provider!r}.")
        return _embedders[key]
