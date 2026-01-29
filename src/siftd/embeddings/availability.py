"""Embedding availability detection.

Central module for checking whether embedding dependencies are installed.
Used to provide graceful degradation when siftd is installed without
the [embed] extra.
"""

_EMBEDDINGS_AVAILABLE: bool | None = None


def embeddings_available() -> bool:
    """Check if embedding dependencies are installed.

    Returns True if fastembed (and its dependencies) can be imported.
    Result is cached after first check.
    """
    global _EMBEDDINGS_AVAILABLE
    if _EMBEDDINGS_AVAILABLE is None:
        try:
            import fastembed  # noqa: F401

            _EMBEDDINGS_AVAILABLE = True
        except ImportError:
            _EMBEDDINGS_AVAILABLE = False
    return _EMBEDDINGS_AVAILABLE


class EmbeddingsNotAvailable(Exception):
    """Raised when embedding functionality is requested but deps not installed."""

    def __init__(self, operation: str = "This operation"):
        self.operation = operation
        self.message = (
            f"{operation} requires the [embed] extra.\n\n"
            "Install with:\n"
            "  siftd install embed\n\n"
            "Or use FTS5 search instead:\n"
            "  siftd query -s \"your search\""
        )
        super().__init__(self.message)


def require_embeddings(operation: str = "This operation") -> None:
    """Raise EmbeddingsNotAvailable if embedding deps are missing.

    Use this at the start of functions that require embeddings to provide
    a clear error message.

    Args:
        operation: Description of the operation being attempted, used in error message.

    Raises:
        EmbeddingsNotAvailable: If fastembed is not installed.
    """
    if not embeddings_available():
        raise EmbeddingsNotAvailable(operation)
