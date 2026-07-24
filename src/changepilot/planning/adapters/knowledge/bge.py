from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from changepilot.planning.domain.failures import KnowledgeError


class BgeEmbeddingProvider:
    def __init__(
        self,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        *,
        model: Any | None = None,
    ) -> None:
        self._model_name = model_name
        if model is not None:
            self._model = model
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise KnowledgeError(
                "install changepilot[retrieval] to enable BGE embeddings"
            ) from exc
        self._model = SentenceTransformer(model_name)

    @property
    def model_id(self) -> str:
        return self._model_name

    def encode_documents(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        values = self._model.encode(
            list(texts),
            normalize_embeddings=True,
        )
        return tuple(tuple(float(item) for item in row) for row in values)

    def encode_query(self, text: str) -> tuple[float, ...]:
        instruction = "为这个句子生成表示以用于检索相关文章："
        values = self._model.encode(
            [instruction + text],
            normalize_embeddings=True,
        )
        return tuple(float(item) for item in values[0])
