from __future__ import annotations


class SentenceTransformer:
    def __init__(self, model_name: str) -> None:
        if model_name == "raise-load-error":
            raise RuntimeError("fake model load error")
        self.model_name = model_name

    def encode(
        self,
        texts: list[str],
        batch_size: int = 64,
        normalize_embeddings: bool = True,
    ) -> list[list[float]]:
        return [[0.0, 1.0, 0.0] for _ in texts]
