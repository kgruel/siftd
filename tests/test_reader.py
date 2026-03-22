"""Tests for siftd.peek.reader — session file reading utilities."""

from pathlib import Path
from types import ModuleType

import pytest

from siftd.peek.reader import (
    AmbiguousSessionError,
    _fallback_tail,
    _find_adapter_for_file,
    _resolve_peek_scan,
    read_session_detail,
    tail_session,
)
from siftd.peek.types import PeekScanResult
from siftd.plugin_discovery import PluginInfo

_SCAN_OK = PeekScanResult(
    session_id="s1", workspace_path="/test",
    last_activity_at="2024-01-01T10:00:00Z", exchange_count=1,
)


def _mod(name="m", **kw):
    m = ModuleType(name)
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def _wire(monkeypatch, m):
    monkeypatch.setattr(
        "siftd.peek.reader.load_all_adapters",
        lambda: [PluginInfo(name="test", origin="builtin", module=m)],
    )


def _adapter(tmp_path, monkeypatch, **kw):
    """Create adapter + session file, wire into reader, return (module, file_path)."""
    loc = tmp_path / "sessions"
    loc.mkdir(exist_ok=True)
    kw.setdefault("DEFAULT_LOCATIONS", [str(loc)])
    m = _mod("a", NAME="test", **kw)
    _wire(monkeypatch, m)
    f = loc / "test.jsonl"
    f.write_text("{}")
    return m, f


class TestResolvePeekScan:
    def test_explicit(self):
        def fn(p): return None
        assert _resolve_peek_scan(_mod(peek_scan=fn)) is fn

    def test_none(self):
        assert _resolve_peek_scan(_mod()) is None

    def test_derived(self):
        assert callable(_resolve_peek_scan(_mod(normalize_record=lambda r: None)))


class TestFallbackTail:
    def test_raw(self, tmp_path):
        (tmp_path / "s.jsonl").write_text('{"a":1}\n{"b":2}\n{"c":3}\n')
        assert len(_fallback_tail(tmp_path / "s.jsonl", 2, True)) == 2

    def test_pretty(self, tmp_path):
        (tmp_path / "s.jsonl").write_text('{"key":"value"}\n')
        r = _fallback_tail(tmp_path / "s.jsonl", 1, False)
        assert r and ("  " in r[0] or "key" in r[0])

    def test_non_json(self, tmp_path):
        (tmp_path / "s.jsonl").write_text("not json\n")
        assert _fallback_tail(tmp_path / "s.jsonl", 1, False) == ["not json"]


class TestAmbiguousSessionError:
    def test_str(self):
        assert "2 files" in str(AmbiguousSessionError("abc", [Path("/a"), Path("/b")]))

    def test_truncated(self):
        assert "5 more" in str(AmbiguousSessionError("x", [Path(f"/{i}") for i in range(15)]))


class TestFindAdapterForFile:
    def test_no_adapters(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.peek.reader.load_all_adapters", lambda: [])
        assert _find_adapter_for_file(tmp_path / "x.jsonl") is None

    def test_by_location(self, tmp_path, monkeypatch):
        m, f = _adapter(tmp_path, monkeypatch, peek_scan=lambda p: None)
        assert _find_adapter_for_file(f) is m

    def test_by_can_handle(self, tmp_path, monkeypatch):
        m = _mod("ch", peek_scan=lambda p: None, can_handle=lambda s: True, DEFAULT_LOCATIONS=[])
        _wire(monkeypatch, m)
        assert _find_adapter_for_file(tmp_path / "x.jsonl") is m

    def test_skips_no_hooks(self, tmp_path, monkeypatch):
        m = _mod("bare", DEFAULT_LOCATIONS=[str(tmp_path)])
        _wire(monkeypatch, m)
        assert _find_adapter_for_file(tmp_path / "x.jsonl") is None

    def test_location_oserror(self, tmp_path, monkeypatch):
        """L313-314: OSError in location resolution is skipped."""
        m = _mod("e", peek_scan=lambda p: None,
                 DEFAULT_LOCATIONS=["/\x00invalid"])  # null byte causes OSError
        _wire(monkeypatch, m)
        assert _find_adapter_for_file(tmp_path / "x.jsonl") is None

    def test_can_handle_exception(self, tmp_path, monkeypatch):
        """L325-326: exception in can_handle is skipped."""
        def bad_handle(s): raise RuntimeError("boom")
        m = _mod("e", peek_scan=lambda p: None, can_handle=bad_handle, DEFAULT_LOCATIONS=[])
        _wire(monkeypatch, m)
        assert _find_adapter_for_file(tmp_path / "x.jsonl") is None


class TestTailSession:
    def test_no_adapter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.peek.reader.load_all_adapters", lambda: [])
        assert tail_session(tmp_path / "x.jsonl") == []

    def test_fallback(self, tmp_path, monkeypatch):
        m, f = _adapter(tmp_path, monkeypatch, peek_scan=lambda p: None)
        f.write_text('{"a":1}\n')
        assert len(tail_session(f, lines=1)) >= 1

    def test_dict_raw(self, tmp_path, monkeypatch):
        m, f = _adapter(tmp_path, monkeypatch, peek_scan=lambda p: None,
                        peek_tail=lambda p, n: [{"k": "v"}])
        r = tail_session(f, lines=1, raw=True)
        assert len(r) == 1 and '"k"' in r[0]

    def test_dict_pretty(self, tmp_path, monkeypatch):
        m, f = _adapter(tmp_path, monkeypatch, peek_scan=lambda p: None,
                        peek_tail=lambda p, n: [{"k": "v"}])
        assert "  " in tail_session(f, lines=1, raw=False)[0]

    def test_non_dict(self, tmp_path, monkeypatch):
        m, f = _adapter(tmp_path, monkeypatch, peek_scan=lambda p: None,
                        peek_tail=lambda p, n: ["line"])
        assert tail_session(f, lines=1) == ["line"]

    def test_error_fallback(self, tmp_path, monkeypatch):
        def bad(p, n): raise RuntimeError("boom")
        m, f = _adapter(tmp_path, monkeypatch, peek_scan=lambda p: None, peek_tail=bad)
        f.write_text('{"x":1}\n')
        assert len(tail_session(f, lines=1)) >= 1


class TestFindSessionFile:
    def _df(self, base, stem, mod=None):
        from siftd.peek.scanner import DiscoveredFile
        base.mkdir(parents=True, exist_ok=True)
        p = base / f"{stem}.jsonl"
        p.write_text("{}")
        return DiscoveredFile(path=p, mtime=1000.0,
                              adapter_module=mod or _mod(), adapter_name="t")

    def _find(self, monkeypatch, files, prefix="abc"):
        from siftd.peek.reader import find_session_file
        monkeypatch.setattr("siftd.peek.reader._discover_files", lambda **kw: files)
        return find_session_file(prefix)

    def test_match_by_stem(self, tmp_path, monkeypatch):
        df = self._df(tmp_path, "abc123")
        assert self._find(monkeypatch, [df]) == df.path

    def test_no_match(self, tmp_path, monkeypatch):
        assert self._find(monkeypatch, [self._df(tmp_path, "xyz")]) is None

    def test_scan_exception(self, tmp_path, monkeypatch):
        """L217-218: peek_scan error during stem match — still matches."""
        def bad(p): raise RuntimeError("boom")
        df = self._df(tmp_path, "abc123", mod=_mod("s", peek_scan=bad))
        assert self._find(monkeypatch, [df]) == df.path

    def test_scan_match_exception(self, tmp_path, monkeypatch):
        """L325-326: peek_scan error during scan-based match."""
        def bad(p): raise RuntimeError("boom")
        df = self._df(tmp_path, "xyz_abc", mod=_mod("s", peek_scan=bad))
        assert self._find(monkeypatch, [df]) is None

    def test_ambiguous(self, tmp_path, monkeypatch):
        d1, d2 = self._df(tmp_path / "a", "abc1"), self._df(tmp_path / "b", "abc2")
        with pytest.raises(AmbiguousSessionError):
            self._find(monkeypatch, [d1, d2])


class TestReadSessionDetail:
    def test_no_adapter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.peek.reader.load_all_adapters", lambda: [])
        assert read_session_detail(tmp_path / "x.jsonl") is None

    def test_missing_file(self, tmp_path, monkeypatch):
        _adapter(tmp_path, monkeypatch, peek_scan=lambda p: None)
        assert read_session_detail(tmp_path / "sessions" / "gone.jsonl") is None

    def test_scan_none(self, tmp_path, monkeypatch):
        m, f = _adapter(tmp_path, monkeypatch, peek_scan=lambda p: None)
        assert read_session_detail(f) is None

    def test_no_exchange_hook(self, tmp_path, monkeypatch):
        m, f = _adapter(tmp_path, monkeypatch, peek_scan=lambda p: _SCAN_OK)
        r = read_session_detail(f)
        assert r is not None and r.exchanges == []

    def test_with_exchanges(self, tmp_path, monkeypatch):
        from siftd.domain.peek import PeekExchange
        ex = PeekExchange(prompt_text="hi", timestamp="2024-01-01T10:00:00Z")
        m, f = _adapter(tmp_path, monkeypatch, peek_scan=lambda p: _SCAN_OK,
                        peek_exchanges=lambda p, n: [ex])
        r = read_session_detail(f)
        assert r is not None and len(r.exchanges) == 1

    def test_exchange_error(self, tmp_path, monkeypatch):
        def bad(p, n): raise RuntimeError("boom")
        m, f = _adapter(tmp_path, monkeypatch, peek_scan=lambda p: _SCAN_OK,
                        peek_exchanges=bad)
        r = read_session_detail(f)
        assert r is not None and r.exchanges == []

    def test_last_n_clamped(self, tmp_path, monkeypatch):
        m, f = _adapter(tmp_path, monkeypatch, peek_scan=lambda p: _SCAN_OK)
        assert read_session_detail(f, last_n=0) is not None

    def test_derived_exchanges(self, tmp_path, monkeypatch):
        """L80-95: adapter with normalize_record but no peek_exchanges."""
        import json

        from siftd.adapters.sdk import NormalizedRecord

        def norm(record):
            kind = record.get("type")
            if kind in ("user", "assistant"):
                return NormalizedRecord(
                    kind=kind,
                    content_blocks=[{"type": "text", "text": record.get("text", "")}],
                    timestamp=record.get("ts"),
                )
            return None

        m, f = _adapter(tmp_path, monkeypatch, peek_scan=lambda p: _SCAN_OK,
                        normalize_record=norm)
        f.write_text(
            json.dumps({"type": "user", "text": "hi", "ts": "2024-01-01T10:00:00Z"}) + "\n"
            + json.dumps({"type": "assistant", "text": "hello", "ts": "2024-01-01T10:01:00Z"}) + "\n"
        )
        r = read_session_detail(f)
        assert r is not None and len(r.exchanges) >= 1
