"""Shared math utilities."""

import numpy as np
from numpy.typing import NDArray


def cosine_similarity(a: list[float] | NDArray, b: list[float] | NDArray) -> float:
    """Compute cosine similarity between two vectors.

    Accepts Python lists or numpy arrays. Returns float in [-1, 1].
    """
    a_arr = np.asarray(a, dtype=np.float32)
    b_arr = np.asarray(b, dtype=np.float32)

    dot = np.dot(a_arr, b_arr)
    norm_a = np.linalg.norm(a_arr)
    norm_b = np.linalg.norm(b_arr)

    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def cosine_similarity_batch(
    query: NDArray, embeddings: NDArray
) -> NDArray:
    """Compute cosine similarity between query and all embeddings at once.

    Args:
        query: 1D array of shape (dim,) - the query embedding.
        embeddings: 2D array of shape (n, dim) - stored embeddings.

    Returns:
        1D array of shape (n,) with similarity scores.
    """
    # Normalize query
    query_norm = np.linalg.norm(query)
    if query_norm == 0:
        return np.zeros(embeddings.shape[0], dtype=np.float32)
    query_normalized = query / query_norm

    # Normalize embeddings (row-wise)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    # Avoid division by zero
    norms = np.where(norms == 0, 1, norms)
    embeddings_normalized = embeddings / norms

    # Batch dot product
    return embeddings_normalized @ query_normalized
