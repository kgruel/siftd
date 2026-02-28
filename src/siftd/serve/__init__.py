"""HTTP team sync server.

Requires the ``[serve]`` optional extra::

    pip install siftd[serve]
"""

from __future__ import annotations


def require_serve(feature: str = "siftd[serve]") -> None:
    """Raise if serve dependencies are not installed."""
    try:
        import litestar  # noqa: F401
    except ImportError:
        raise ImportError(
            f"{feature} requires the [serve] extra. "
            "Install with: pip install siftd[serve]"
        ) from None
