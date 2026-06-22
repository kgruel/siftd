"""Tests for siftd.peek.scanner — session discovery utilities."""

from pathlib import Path
from types import ModuleType

from siftd.domain.peek import SessionInfo
from siftd.peek.scanner import (
    DiscoveredFile,
    _disambiguate_workspace_names,
    _disambiguated_name,
    _discover_files,
    _get_glob_patterns,
    _matches_branch,
    _matches_workspace,
    _scan_session_file,
)
from siftd.peek.types import PeekScanResult


def _session(ws_path=None, ws_name=None, branch=None):
    return SessionInfo(
        session_id="s1", file_path=Path("/tmp/s.jsonl"),
        workspace_path=ws_path, workspace_name=ws_name, branch=branch,
    )


def _mod(name="m", **kw):
    m = ModuleType(name)
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def _discovered(path=None, mtime=1000.0, mod=None, name="test"):
    return DiscoveredFile(
        path=path or Path("/tmp/test.jsonl"),
        mtime=mtime, adapter_module=mod or _mod(), adapter_name=name,
    )


class TestMatchesWorkspace:
    def test_name(self):
        assert _matches_workspace(_session(ws_name="siftd"), "siftd")

    def test_path(self):
        assert _matches_workspace(_session(ws_path="/home/user/siftd"), "siftd")

    def test_case_insensitive(self):
        assert _matches_workspace(_session(ws_name="Siftd"), "siftd")

    def test_no_match(self):
        assert not _matches_workspace(_session(ws_name="other"), "siftd")

    def test_none(self):
        assert not _matches_workspace(_session(), "siftd")


class TestMatchesBranch:
    def test_match(self):
        assert _matches_branch(_session(branch="main"), "main")

    def test_case_insensitive(self):
        assert _matches_branch(_session(branch="Feature/X"), "feature")

    def test_no_branch(self):
        assert not _matches_branch(_session(), "main")

    def test_no_match(self):
        assert not _matches_branch(_session(branch="main"), "dev")


class TestGetGlobPatterns:
    def test_custom(self):
        assert _get_glob_patterns(_mod(PEEK_GLOB_PATTERNS=["*.log"])) == ["*.log"]

    def test_jsonl(self):
        assert _get_glob_patterns(_mod(HARNESS_LOG_FORMAT="jsonl")) == ["**/*.jsonl"]

    def test_json(self):
        assert _get_glob_patterns(_mod(HARNESS_LOG_FORMAT="json")) == ["**/*.json"]

    def test_markdown(self):
        assert _get_glob_patterns(_mod(HARNESS_LOG_FORMAT="markdown")) == ["**/*.md"]

    def test_other(self):
        assert _get_glob_patterns(_mod(HARNESS_LOG_FORMAT="xml")) == ["**/*"]

    def test_default(self):
        assert _get_glob_patterns(_mod()) == ["**/*.jsonl"]


class TestDisambiguateWorkspaceNames:
    def test_no_collision(self):
        s = [_session(ws_path="/a/proj", ws_name="proj")]
        _disambiguate_workspace_names(s)
        assert s[0].workspace_name == "proj"

    def test_same_path(self):
        s = [_session(ws_path="/a/p", ws_name="p"), _session(ws_path="/a/p", ws_name="p")]
        _disambiguate_workspace_names(s)
        assert s[0].workspace_name == "p"

    def test_different_paths(self):
        s1 = _session(ws_path="/home/alice/proj", ws_name="proj")
        s2 = _session(ws_path="/home/bob/proj", ws_name="proj")
        _disambiguate_workspace_names([s1, s2])
        assert s1.workspace_name != s2.workspace_name


class TestDisambiguatedName:
    def test_unique_at_parent(self):
        assert _disambiguated_name("/home/a/p", {"/home/a/p", "/home/b/p"}) == "a/p"

    def test_already_unique(self):
        assert _disambiguated_name("/a/x", {"/a/x", "/b/y"}) == "x"

    def test_deep_paths(self):
        assert "x" in _disambiguated_name("/x/a/b/c", {"/x/a/b/c", "/y/a/b/c"})


class TestScanSessionFile:
    def test_no_peek_scan(self):
        r = _scan_session_file(_discovered())
        assert r is not None and r.preview_available is False

    def test_derived_peek_scan(self, tmp_path):
        """L169-175: auto-derive peek_scan from normalize_record."""
        import json

        from siftd.adapters.sdk import NormalizedRecord

        def norm(r):
            if r.get("type") in ("user", "assistant"):
                return NormalizedRecord(kind=r["type"], timestamp=r.get("ts"),
                                        content_blocks=[{"type": "text", "text": "x"}])
            return None

        p = tmp_path / "s.jsonl"
        p.write_text(json.dumps({"type": "user", "ts": "2024-01-01T10:00:00Z"}) + "\n"
                     + json.dumps({"type": "assistant", "ts": "2024-01-01T10:01:00Z"}) + "\n")
        r = _scan_session_file(_discovered(path=p, mod=_mod("d", normalize_record=norm)))
        assert r is not None and r.session_id is not None

    def test_success(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("{}")
        m = _mod(peek_scan=lambda path: PeekScanResult(
            session_id="s1", workspace_path="/test",
            last_activity_at="2024-01-01T10:00:00Z", exchange_count=5,
        ))
        r = _scan_session_file(_discovered(path=p, mod=m))
        assert r is not None and r.session_id == "s1" and r.exchange_count == 5

    def test_started_at_threads_through(self, tmp_path):
        """PeekScanResult.started_at reaches SessionInfo — the scan already
        extracts it; list consumers (Sessions live cards) show age from it
        without a second file read."""
        p = tmp_path / "t.jsonl"
        p.write_text("{}")
        m = _mod(peek_scan=lambda path: PeekScanResult(
            session_id="s1", workspace_path="/test",
            started_at="2026-06-11T09:00:00Z",
            last_activity_at="2026-06-11T10:00:00Z", exchange_count=5,
        ))
        r = _scan_session_file(_discovered(path=p, mod=m))
        assert r is not None and r.started_at == "2026-06-11T09:00:00Z"

    def test_returns_none(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("{}")
        m = _mod(peek_scan=lambda path: None)
        assert _scan_session_file(_discovered(path=p, mod=m)) is None

    def test_error(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("{}")

        def fail(path):
            raise RuntimeError("boom")

        m = _mod(peek_scan=fail)
        assert _scan_session_file(_discovered(path=p, mod=m)) is None

    def test_invalid_timestamp(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("{}")
        m = _mod(peek_scan=lambda path: PeekScanResult(
            session_id="s2", workspace_path="/test",
            last_activity_at="not-a-date", exchange_count=1,
        ))
        r = _scan_session_file(_discovered(path=p, mtime=999.0, mod=m))
        assert r is not None and r.last_activity == 999.0


class TestDiscoverFilesContainment:
    """Scanner must not discover files that escape the adapter's DEFAULT_LOCATIONS via symlinks."""

    def _make_adapter(self, location: Path) -> object:
        from siftd.plugin_discovery import PluginInfo

        m = _mod("peek_adapter", normalize_record=lambda r: None)
        m.DEFAULT_LOCATIONS = [str(location)]
        m.PEEK_GLOB_PATTERNS = ["*.jsonl"]
        return PluginInfo(name="fake", origin="builtin", module=m)

    def test_symlink_escaping_location_is_not_discovered(self, monkeypatch, tmp_path):
        base = tmp_path / "sessions"
        base.mkdir()
        outside = tmp_path / "outside" / "secret.jsonl"
        outside.parent.mkdir()
        outside.write_text("{}")
        link = base / "escape.jsonl"
        link.symlink_to(outside)

        monkeypatch.setattr(
            "siftd.peek.scanner.load_all_adapters",
            lambda: [self._make_adapter(base)],
        )
        found = _discover_files(threshold_seconds=9999, include_inactive=True)
        assert not any("escape" in str(f.path) for f in found)

    def test_symlink_inside_location_is_discovered(self, monkeypatch, tmp_path):
        base = tmp_path / "sessions"
        base.mkdir()
        real = base / "real.jsonl"
        real.write_text("{}")
        link = base / "linked.jsonl"
        link.symlink_to(real)

        monkeypatch.setattr(
            "siftd.peek.scanner.load_all_adapters",
            lambda: [self._make_adapter(base)],
        )
        found = _discover_files(threshold_seconds=9999, include_inactive=True)
        assert any("linked" in str(f.path) for f in found)
