"""
Tests for vector search backend protocol.

Verify that stub backends conforming to the protocol pass isinstance checks
and are callable.
"""

import pytest

from kestrel_sdk.timeline import VectorSearchBackend


class StubVectorSearchBackend:
    """Stub vector search backend for testing protocol conformance."""

    def __init__(self, supports_filters: bool = True):
        self._supports_filters = supports_filters

    async def knn(
        self,
        query_embedding: bytes,
        k: int,
        filter: dict | None = None,
    ) -> list[tuple[str, float]]:
        """Return hard-coded results for testing."""
        # Simulate returning top-k results
        results = [
            ("event-1", 0.95),
            ("event-2", 0.89),
            ("event-3", 0.82),
        ]
        return results[:k]

    @property
    def supports_filters(self) -> bool:
        """Return configured filter support."""
        return self._supports_filters


@pytest.mark.asyncio
async def test_vector_search_backend_conformance():
    """A stub backend with required methods satisfies VectorSearchBackend."""
    backend = StubVectorSearchBackend()
    assert isinstance(backend, VectorSearchBackend)


@pytest.mark.asyncio
async def test_vector_search_backend_callable():
    """VectorSearchBackend.knn is callable and returns expected format."""
    backend = StubVectorSearchBackend()

    # Create a dummy embedding (1536 float32 values = 6144 bytes)
    embedding = b"\x00" * 6144

    results = await backend.knn(
        query_embedding=embedding,
        k=2,
        filter={"timeline_id": "timeline-123"},
    )

    assert isinstance(results, list)
    assert len(results) == 2
    assert results[0] == ("event-1", 0.95)
    assert results[1] == ("event-2", 0.89)


@pytest.mark.asyncio
async def test_vector_search_backend_without_filter():
    """VectorSearchBackend.knn works without filter parameter."""
    backend = StubVectorSearchBackend()
    embedding = b"\x00" * 6144

    results = await backend.knn(query_embedding=embedding, k=3)

    assert len(results) == 3
    assert all(isinstance(r, tuple) and len(r) == 2 for r in results)
    assert all(isinstance(r[0], str) and isinstance(r[1], float) for r in results)


@pytest.mark.asyncio
async def test_vector_search_backend_supports_filters_property():
    """VectorSearchBackend.supports_filters is accessible."""
    backend_with_filters = StubVectorSearchBackend(supports_filters=True)
    assert backend_with_filters.supports_filters is True

    backend_without_filters = StubVectorSearchBackend(supports_filters=False)
    assert backend_without_filters.supports_filters is False


@pytest.mark.asyncio
async def test_vector_search_backend_returns_tuples():
    """VectorSearchBackend.knn returns list of (str, float) tuples."""
    backend = StubVectorSearchBackend()
    embedding = b"\x00" * 6144

    results = await backend.knn(query_embedding=embedding, k=3)

    for row_id, similarity in results:
        assert isinstance(row_id, str)
        assert isinstance(similarity, float)
        assert 0.0 <= similarity <= 1.0
