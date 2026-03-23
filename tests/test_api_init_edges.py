import pytest

import siftd.api as api


def test_api_getattr_unknown_symbol_raises_attribute_error():
    with pytest.raises(AttributeError, match="has no attribute"):
        getattr(api, "__definitely_missing_symbol__")
