from pathlib import Path
from types import ModuleType

from siftd.peek.reader import _resolve_peek_scan


def test_resolve_peek_scan_derived_function_invokes_sdk_scan(monkeypatch):
    mod = ModuleType("m")
    mod.normalize_record = lambda _r: None

    monkeypatch.setattr("siftd.adapters.sdk.iter_jsonl", lambda p: [{"x": 1}])
    monkeypatch.setattr(
        "siftd.adapters.sdk.peek_scan_from_records",
        lambda records, norm, **kwargs: {
            "records": list(records),
            "norm_is_module": norm is mod.normalize_record,
            "kwargs": kwargs,
        },
    )

    fn = _resolve_peek_scan(mod)
    out = fn(Path("/tmp/s1.jsonl"))

    assert out["records"] == [{"x": 1}]
    assert out["norm_is_module"]
    assert out["kwargs"]["default_session_id"] == "s1"
