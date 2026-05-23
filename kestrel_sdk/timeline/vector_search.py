"""
Vector search backend protocol for semantic timeline search.

Pluggable vector search backend (pgvector for PostgreSQL, pure-Python cosine
for SQLite). Story-archive will ship two concrete implementations behind this
Protocol.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class VectorSearchBackend(Protocol):
    """
    Protocol for pluggable vector search backends.

    Implementations can use pgvector (PostgreSQL), pure-Python cosine similarity
    (SQLite), or other vector search engines.
    """

    async def knn(
        self,
        query_embedding: bytes,
        k: int,
        filter: dict | None = None,
    ) -> list[tuple[str, float]]:
        """
        K-nearest neighbors search for similar embeddings.

        Args:
            query_embedding: Packed float32 embedding bytes (1536 dims).
                BLOB column type maps to bytes naturally across dialects.
            k: Number of nearest neighbors to return
            filter: Optional filter dict (e.g., {"timeline_id": "..."})

        Returns:
            List of (row_id, similarity_score) tuples, ordered by similarity
            (highest first). Similarity scores are typically in [0.0, 1.0]
            for cosine similarity.

        Example:
            ```python
            results = await backend.knn(
                query_embedding=embedding_bytes,
                k=10,
                filter={"timeline_id": "timeline-123"}
            )
            # Returns: [("event-5", 0.95), ("event-12", 0.89), ...]
            ```
        """
        ...

    @property
    def supports_filters(self) -> bool:
        """
        Whether this backend supports filter parameters.

        Backends like pgvector can efficiently filter at query time.
        Pure-Python backends may need to filter after retrieval.
        """
        ...
