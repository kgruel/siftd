"""HTTP team sync server.

Requires the ``[serve]`` optional extra.  Install via ``siftd install serve``
or directly with the appropriate package manager command.
"""

from __future__ import annotations


def require_serve(feature: str = "siftd[serve]") -> None:
    """Raise if serve dependencies are not installed."""
    for dep in ("litestar", "uvicorn"):
        try:
            __import__(dep)
        except ImportError:
            raise ImportError(
                f"{feature} requires the [serve] extra."
            ) from None
