"""Protocol definition for embedding service providers."""

from __future__ import annotations

import asyncio
import struct
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence


def pack_embedding(embedding: Any) -> bytes:
    """Serialize an embedding vector as packed float32 bytes for DB storage.

    Accepts numpy arrays or list[float]. Uses struct packing instead of pickle
    to avoid deserialization attack vectors if the DB is compromised.
    """
    values: list[float] = embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
    return struct.pack(f"<{len(values)}f", *values)


def unpack_embedding(blob: bytes) -> list[float]:
    """Deserialize an embedding vector from DB storage.

    Deserializes from the struct-packed float32 format.

    Legacy pickle-serialized embeddings are no longer supported due to
    security concerns (unsafe deserialization). Run a migration to
    re-encode any old embeddings before upgrading.
    """
    try:
        count = len(blob) // 4  # 4 bytes per float32
        return list(struct.unpack(f"<{count}f", blob))
    except struct.error as exc:
        raise ValueError(
            "Failed to unpack embedding blob as float32 array. "
            "If this is a legacy pickle-serialized embedding, it must be "
            "migrated to struct-packed format first."
        ) from exc


class EmbeddingSerializationMixin:
    """Default serialize/deserialize implementations shared by all embedding providers."""

    def serialize_embedding(self, embedding: Any) -> bytes:
        return pack_embedding(embedding)

    def deserialize_embedding(self, blob: bytes) -> list[float]:
        return unpack_embedding(blob)

    async def generate_embeddings_batch(
        self,
        texts: Sequence[str],
        *,
        language: str | None = None,
        task_type: str | None = None,
    ) -> list[Any]:
        """Generate embeddings for multiple texts. Default: parallel single calls."""
        return list(
            await asyncio.gather(
                *(
                    self.generate_embedding(t, language=language, task_type=task_type)  # type: ignore[attr-defined]
                    for t in texts
                )
            )
        )


@runtime_checkable
class EmbeddingServiceProtocol(Protocol):
    """Interface that all embedding providers must satisfy."""

    async def generate_embedding(
        self, text: str, *, language: str | None = None, task_type: str | None = None
    ) -> Any: ...

    async def generate_embeddings_batch(
        self,
        texts: Sequence[str],
        *,
        language: str | None = None,
        task_type: str | None = None,
    ) -> list[Any]: ...

    def serialize_embedding(self, embedding: Any) -> bytes: ...

    def deserialize_embedding(self, blob: bytes) -> list[float]: ...

    def get_model_name(self, language: str | None = None) -> str: ...

    def get_dimensions(self, language: str | None = None) -> int: ...

    async def get_dimensions_async(self, language: str | None = None) -> int: ...

    def close(self) -> None: ...

    async def aclose(self) -> None: ...
