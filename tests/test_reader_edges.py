from pathlib import Path
from types import ModuleType

from siftd.peek.reader import _resolve_peek_scan


def test_resolve_peek_scan_derived_function_invokes_sdk_scan(monkeypatch):
    mod = ModuleType("m")
    mod.normalize_record = lambda _r: None
    monkeypatch.setattr("siftd.adapters.sdk.iter_jsonl", lambda p: [{"x": 1}])
    monkeypatch.setattr(
        "siftd.adapters.sdk.peek_scan_from_records",
        lambda records, norm, **kwargs: (list(records), norm is mod.normalize_record, kwargs["default_session_id"]),
    )
    assert _resolve_peek_scan(mod)(Path("/tmp/s1.jsonl")) == ([{"x": 1}], True, "s1")
