from __future__ import annotations

from typing import Callable, List, Protocol, Tuple
import math
from urllib.parse import urlsplit, urlunsplit


from vam.config import get_settings

class VisionBackend(Protocol):
    name: str

    def embed_text(self, text: str, *, instruction: str) -> Tuple[List[float], int]: ...
    def embed_texts_batch(
        self, texts: List[str], *, instruction: str, batch_size: int
    ) -> Tuple[List[List[float]], int]: ...
    def embed_image_base64(self, image_base64_or_data_uri: str, *, instruction: str) -> Tuple[List[float], int]: ...
    def embed_images_base64_batch(
        self, images_base64_or_data_uris: List[str], *, instruction: str, batch_size: int
    ) -> Tuple[List[List[float]], int]: ...


def validate_embeddings(vectors, *, expected_count, expected_dim=None):
    if not isinstance(vectors, list) or len(vectors) != expected_count:
        raise RuntimeError("embedding response count mismatch")
    if not vectors:
        return vectors
    dim = len(vectors[0]) if expected_dim is None else expected_dim
    if dim <= 0:
        raise RuntimeError("embedding dimension must be positive")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dim:
            raise RuntimeError("embedding dim mismatch")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
            raise RuntimeError("embedding contains non-finite or non-numeric values")
        if any(abs(v) > 65504 for v in vector):
            raise RuntimeError("embedding cannot be represented by FP16 storage")
    return vectors


class OpenRouterGeminiBackend:
    name = "siliconflow-qwen"

    def fingerprint(self):
        cfg = get_settings()
        url = urlsplit((cfg.embedding_base_url_override or cfg.chat_base_url).rstrip("/"))
        host = url.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if url.port:
            host += ":" + str(url.port)
        endpoint = urlunsplit((url.scheme, host, url.path, "", ""))
        return {"provider": cfg.provider_name if not cfg.embedding_base_url_override else "override",
                "endpoint": endpoint,
                "model": cfg.embedding_model_id, "encoding_version": 1}

    def embed_text(self, text: str, *, instruction: str) -> Tuple[List[float], int]:
        from vam.llm.openrouter import OpenRouterEmbeddingsClient
        client = OpenRouterEmbeddingsClient()
        vectors, dim = client.generate_text_embeddings([text], input_type="search_query" if "query" in instruction.lower() else "search_document")
        return validate_embeddings(vectors, expected_count=1, expected_dim=dim)[0], dim

    def embed_texts_batch(self, texts: List[str], *, instruction: str, batch_size: int) -> Tuple[List[List[float]], int]:
        from ..llm.openrouter import OpenRouterEmbeddingsClient
        client = OpenRouterEmbeddingsClient()
        return client.generate_text_embeddings(texts, input_type="search_query" if "query" in instruction.lower() else "search_document")

    def embed_image_base64(self, image_base64_or_data_uri: str, *, instruction: str) -> Tuple[List[float], int]:
        from ..llm.openrouter import OpenRouterEmbeddingsClient
        client = OpenRouterEmbeddingsClient()
        vectors, dim = client.generate_image_embeddings([image_base64_or_data_uri])
        return validate_embeddings(vectors, expected_count=1, expected_dim=dim)[0], dim

    def embed_images_base64_batch(
        self, images_base64_or_data_uris: List[str], *, instruction: str, batch_size: int
    ) -> Tuple[List[List[float]], int]:
        from ..llm.openrouter import OpenRouterEmbeddingsClient
        client = OpenRouterEmbeddingsClient()
        return client.generate_image_embeddings(images_base64_or_data_uris)


class DummyBackend:
    name = "dummy"

    def embed_text(self, text: str, *, instruction: str) -> Tuple[List[float], int]:
        return [0.0] * 128, 128

    def embed_texts_batch(self, texts: List[str], *, instruction: str, batch_size: int) -> Tuple[List[List[float]], int]:
        n = len(texts)
        return [[0.0] * 128 for _ in range(n)], 128

    def embed_image_base64(self, image_base64_or_data_uri: str, *, instruction: str) -> Tuple[List[float], int]:
        return [0.0] * 128, 128

    def embed_images_base64_batch(
        self, images_base64_or_data_uris: List[str], *, instruction: str, batch_size: int
    ) -> Tuple[List[List[float]], int]:
        n = len(images_base64_or_data_uris)
        return [[0.0] * 128 for _ in range(n)], 128


def get_backend() -> VisionBackend:
    if not get_settings().enable_retrieval:
        return DummyBackend()
    return OpenRouterGeminiBackend()
