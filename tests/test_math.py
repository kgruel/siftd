"""Tests for siftd.math module."""

import numpy as np
import pytest

from siftd.math import cosine_similarity, cosine_similarity_batch


class TestCosineSimilarity:
    def test_identical(self):
        assert cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_opposite(self):
        assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_zero_vector(self):
        assert cosine_similarity([0, 0], [1, 0]) == 0.0

    def test_numpy_arrays(self):
        a = np.array([1.0, 0.0], dtype=np.float32)
        b = np.array([0.7, 0.7], dtype=np.float32)
        assert 0 < cosine_similarity(a, b) < 1


class TestCosineSimilarityBatch:
    def test_single(self):
        query = np.array([1.0, 0.0], dtype=np.float32)
        embeddings = np.array([[1.0, 0.0]], dtype=np.float32)
        result = cosine_similarity_batch(query, embeddings)
        assert result[0] == pytest.approx(1.0)

    def test_multiple(self):
        query = np.array([1.0, 0.0], dtype=np.float32)
        embeddings = np.array([[1, 0], [0, 1], [-1, 0]], dtype=np.float32)
        result = cosine_similarity_batch(query, embeddings)
        assert result[0] == pytest.approx(1.0)
        assert result[1] == pytest.approx(0.0)
        assert result[2] == pytest.approx(-1.0)

    def test_zero_query(self):
        query = np.zeros(3, dtype=np.float32)
        embeddings = np.array([[1, 0, 0]], dtype=np.float32)
        result = cosine_similarity_batch(query, embeddings)
        assert result[0] == pytest.approx(0.0)

    def test_zero_embedding(self):
        query = np.array([1.0, 0.0], dtype=np.float32)
        embeddings = np.array([[0.0, 0.0]], dtype=np.float32)
        result = cosine_similarity_batch(query, embeddings)
        assert result.shape == (1,)
