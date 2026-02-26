"""Tests for siftd serve — HTTP team sync server."""

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve
