"""Tests for config module."""

import argparse

import pytest
import tomlkit

from siftd import config as cfg
from siftd.cli_common import apply_config_defaults
from siftd.cli_search import _has_explicit_formatter
from siftd.config import (
    _coerce_value,
    _ensure_parent_table,
    _get_parent_table,
    _is_bool_like,
    _is_int_like,
    _is_str,
    _is_str_list,
    _iter_config_items,
    _match_schema,
    _validate_config,
    append_config_list,
    get_adapter_locations,
    get_config,
    get_ingestion_filter_binary,
    get_query_defaults,
    get_search_defaults,
    get_ssh_options,
    get_sync_remote,
    get_sync_remotes,
    get_tools_defaults,
    load_config,
    remove_config_list,
    remove_sync_remote,
    set_config,
    set_remote_auth,
    set_sync_remote,
    update_last_pull,
    update_last_push,
)
from siftd.paths import db_path

_L = "adapters.claude_code.locations"  # frequently used key


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "siftd"


def _w(config_dir, text):
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(text)


class TestSchema:
    @pytest.mark.parametrize("val,expected", [("hello", True), (42, False), (None, False)])
    def test_is_str(self, val, expected):
        assert _is_str(val) is expected

    @pytest.mark.parametrize("val,expected", [
        (True, False), (False, False), (42, True), (0, True),
        ("42", True), ("-1", True), ("abc", False), (3.14, False), (None, False),
    ])
    def test_is_int_like(self, val, expected):
        assert _is_int_like(val) is expected

    @pytest.mark.parametrize("val,expected", [
        (True, True), (False, True), ("true", True), ("false", True),
        ("0", True), ("1", True), ("yes", True), ("no", True),
        (" True ", True), (" NO ", True), ("maybe", False), (42, False), (None, False),
    ])
    def test_is_bool_like(self, val, expected):
        assert _is_bool_like(val) is expected

    @pytest.mark.parametrize("val,expected", [
        (["a", "b"], True), ([], True), ([1, 2], False), ("x", False),
    ])
    def test_is_str_list(self, val, expected):
        assert _is_str_list(val) is expected

    @pytest.mark.parametrize("key,typ", [("search.formatter", "string"), (_L, "list[string]")])
    def test_match(self, key, typ):
        assert _match_schema(key).expected == typ

    def test_no_match(self):
        assert _match_schema("totally.unknown.deep.key") is None

    def test_iter_config_items(self):
        keys = [k for k, _ in _iter_config_items({"search": {"formatter": "json"}})]
        assert "search" in keys and "search.formatter" in keys
        assert list(_iter_config_items("not a dict")) == []

    def test_validate_warns(self, capsys):
        doc = tomlkit.parse('unknown_key = "v"\n[query]\nlimit = "x"\n')
        _validate_config(doc)
        err = capsys.readouterr().err
        assert "Unknown config key" in err and "expects int" in err

    def test_validate_clean(self, capsys):
        _validate_config(tomlkit.parse('[search]\nformatter = "verbose"\n'))
        assert capsys.readouterr().err == ""

    def test_ensure_parent(self):
        doc = tomlkit.document()
        _ensure_parent_table(doc, ["sync", "remotes", "leaf"])
        assert "remotes" in doc["sync"]
        with pytest.raises(ValueError, match="is not a table"):
            _ensure_parent_table(tomlkit.parse('search = "x"\n'), ["search", "f"])

    def test_get_parent(self):
        assert _get_parent_table(tomlkit.document(), ["a", "b", "c"]) is None
        doc = tomlkit.parse('[sync.remotes]\nbox = "str"\n')
        assert _get_parent_table(doc, ["sync", "remotes", "box", "leaf"]) is None


class TestConfigCRUD:
    def test_load(self, config_dir, capsys):
        assert len(load_config()) == 0
        _w(config_dir, '[search]\nformatter = "verbose"\n')
        assert load_config()["search"]["formatter"] == "verbose"
        _w(config_dir, "invalid [ toml")
        assert len(load_config()) == 0
        assert "Warning" in capsys.readouterr().err

    @pytest.mark.parametrize("key,expected", [
        ("search.formatter", "json"), ("search.nonexistent", None),
        ("nonexistent.key", None), ("search", None), (_L, None),
    ])
    def test_get(self, config_dir, key, expected):
        _w(config_dir, '[adapters.claude_code]\nlocations = ["/a"]\n[search]\nformatter = "json"\n')
        assert get_config(key) == expected

    def test_set(self, config_dir):
        set_config("search.formatter", "verbose")
        assert "verbose" in (config_dir / "config.toml").read_text()
        set_config("search.formatter", "json")
        assert get_config("search.formatter") == "json"

    def test_set_preserves_comments(self, config_dir):
        _w(config_dir, '# My config\n[search]\nformatter = "json"\n')
        set_config("query.limit", "20")
        content = (config_dir / "config.toml").read_text()
        assert "# My config" in content and "json" in content

    def test_set_rejects_unknown(self, config_dir):
        with pytest.raises(ValueError, match="Unknown config key"):
            set_config("search.limit", "20")

    def test_set_corrupt_recovery(self, config_dir):
        _w(config_dir, "invalid [ toml")
        set_config("search.formatter", "json")
        assert get_config("search.formatter") == "json"

    @pytest.mark.parametrize("val,expected", [
        ("true", True), ("TRUE", True), ("false", False), ("FALSE", False),
        ("verbose", "verbose"), ("20", "20"), ("yes", "yes"), ("", ""),
    ])
    def test_coerce(self, val, expected):
        assert _coerce_value(val) == expected

    def test_coercion_roundtrip(self, config_dir):
        set_config("ingestion.filter_binary", "false")
        assert load_config()["ingestion"]["filter_binary"] is False
        set_config("ingestion.filter_binary", "true")
        assert load_config()["ingestion"]["filter_binary"] is True
        set_config("search.formatter", "verbose")
        assert isinstance(load_config()["search"]["formatter"], str)


class TestConfigListOps:
    def test_append_lifecycle(self, config_dir):
        assert append_config_list(_L, "/a") is True
        assert load_config()["adapters"]["claude_code"]["locations"] == ["/a"]
        assert append_config_list(_L, "/a") is False  # dedup
        assert append_config_list(_L, "/b") is True
        assert "/b" in load_config()["adapters"]["claude_code"]["locations"]

    def test_append_corrupt(self, config_dir):
        _w(config_dir, "invalid [ toml")
        assert append_config_list(_L, "/new") is True
        assert load_config()["adapters"]["claude_code"]["locations"] == ["/new"]

    def test_remove_lifecycle(self, config_dir):
        _w(config_dir, '[adapters.claude_code]\nlocations = ["a", "b", "a"]\n')
        assert remove_config_list(_L, "a") is True
        assert load_config()["adapters"]["claude_code"]["locations"] == ["b"]
        assert remove_config_list(_L, "b") is True
        assert load_config()["adapters"]["claude_code"]["locations"] == []
        assert remove_config_list(_L, "b") is False

    @pytest.mark.parametrize("fn", [append_config_list, remove_config_list])
    def test_non_list_raises(self, config_dir, fn):
        _w(config_dir, '[search]\nformatter = "json"\n')
        with pytest.raises(ValueError):
            fn("search.formatter", "x")

    @pytest.mark.parametrize("setup", [None, "invalid [ toml", "[search]\n", "[adapters.claude_code]\n"])
    def test_remove_returns_false(self, config_dir, setup):
        if setup is not None:
            _w(config_dir, setup)
        assert remove_config_list(_L, "x") is False


class TestDefaultsAndLookups:
    def test_search_defaults(self, config_dir):
        assert get_search_defaults() == {}
        _w(config_dir, '[search]\nformatter = "thread"\n')
        assert get_search_defaults() == {"format": "thread"}

    def test_query_defaults(self, config_dir):
        assert get_query_defaults() == {}
        _w(config_dir, "[query]\nlimit = 25\nchars = 300\ntool_chars = 80\n")
        assert get_query_defaults() == {"limit": 25, "chars": 300, "tool_chars": 80}
        _w(config_dir, '[query]\nlimit = "abc"\nchars = 200\n')
        assert "limit" not in get_query_defaults() and get_query_defaults()["chars"] == 200

    def test_tools_defaults(self, config_dir):
        assert get_tools_defaults() == {}
        _w(config_dir, "[tools]\nlimit = 50\n")
        assert get_tools_defaults() == {"limit": 50}
        _w(config_dir, '[tools]\nlimit = "all"\n')
        assert get_tools_defaults() == {}

    def test_adapter_locations(self, config_dir):
        assert get_adapter_locations("nonexistent") is None
        _w(config_dir, '[adapters.claude_code]\nlocations = ["~/.claude", "/other"]\n')
        assert get_adapter_locations("claude_code") == ["~/.claude", "/other"]
        assert get_adapter_locations("unknown") is None

    @pytest.mark.parametrize("toml", ['adapters = "x"\n', '[adapters]\nclaude_code = "x"\n'])
    def test_adapter_locations_type_guards(self, config_dir, toml):
        _w(config_dir, toml)
        assert get_adapter_locations("claude_code") is None

    @pytest.mark.parametrize("toml,expected", [
        (None, True),  # default
        ("[ingestion]\nfilter_binary = false\n", False),
        ("[ingestion]\nfilter_binary = true\n", True),
        ('[ingestion]\nfilter_binary = "false"\n', False),
        ('[ingestion]\nfilter_binary = "no"\n', False),
        ('[ingestion]\nfilter_binary = "0"\n', False),
        ('[ingestion]\nfilter_binary = "yes"\n', True),
    ])
    def test_ingestion_filter(self, config_dir, toml, expected):
        if toml is not None:
            _w(config_dir, toml)
        assert get_ingestion_filter_binary() is expected

    def test_db_path(self, config_dir, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        assert db_path().name == "siftd.db"
        _w(config_dir, '[db]\npath = "/tmp/custom.db"\n')
        assert str(db_path()) == "/tmp/custom.db"
        _w(config_dir, '[db]\npath = "~/my/siftd.db"\n')
        assert str(db_path()).endswith("my/siftd.db") and "~" not in str(db_path())


class TestSyncRemotes:
    def test_lifecycle(self, config_dir):
        set_sync_remote("alcove", "alcove.local", "/data/siftd.db")
        r = get_sync_remotes()[0]
        assert (r["name"], r["host"], r["path"]) == ("alcove", "alcove.local", "/data/siftd.db")
        assert get_sync_remote("nonexistent") is None
        set_remote_auth("alcove", {"token": "abc"})
        assert get_sync_remotes()[0]["auth"] == {"token": "abc"}
        set_sync_remote("alcove", None, "/data/siftd.db")  # remove host
        assert get_sync_remote("alcove")["host"] is None
        assert remove_sync_remote("alcove") is True
        assert len(get_sync_remotes()) == 0
        # No host from start
        set_sync_remote("local", None, "/tmp/siftd.db")
        assert get_sync_remote("local")["host"] is None
        # Remove nonexistent name
        assert remove_sync_remote("nonexistent") is False

    def test_corrupt_recovery(self, config_dir):
        _w(config_dir, "invalid [ toml")
        set_sync_remote("fresh", "h", "/p")
        assert get_sync_remote("fresh") is not None

    @pytest.mark.parametrize("setup", [
        None, "invalid [ toml", '[search]\nformatter = "json"\n', "[sync]\n",
    ])
    def test_remove_error_paths(self, config_dir, setup):
        if setup is not None:
            _w(config_dir, setup)
        assert remove_sync_remote("x") is False

    @pytest.mark.parametrize("toml", [
        'sync = "x"\n', '[sync]\nremotes = "x"\n', '[sync.remotes]\nbad = "x"\n',
    ])
    def test_get_remotes_type_guards(self, config_dir, toml):
        _w(config_dir, toml)
        assert get_sync_remotes() == []

    def test_push_and_pull(self, config_dir):
        set_sync_remote("box", "box.local", "/data/db")
        update_last_push("box", "2026-01-01T00:00:00Z")
        update_last_pull("box", "2026-03-21T12:00:00Z")
        remote = get_sync_remote("box")
        assert remote["last_push"] == "2026-01-01T00:00:00Z"
        assert remote["last_pull"] == "2026-03-21T12:00:00Z"

    @pytest.mark.parametrize("fn_name", ["update_last_push", "update_last_pull"])
    @pytest.mark.parametrize("setup", [
        '[search]\nformatter = "json"\n', "[sync]\n", "invalid [ toml",
    ])
    def test_timestamp_error_paths(self, config_dir, fn_name, setup):
        _w(config_dir, setup)
        getattr(cfg, fn_name)("ghost", "ts")
        assert cfg.get_sync_remote("ghost") is None

    @pytest.mark.parametrize("fn_name", ["update_last_push", "update_last_pull"])
    def test_timestamp_unknown_remote(self, config_dir, fn_name):
        cfg.set_sync_remote("known", "h", "/p")
        getattr(cfg, fn_name)("unknown", "ts")
        assert cfg.get_sync_remote("unknown") is None


class TestSSHOptions:
    def test_global_and_timeout(self, config_dir):
        _w(config_dir, '[sync.ssh]\noptions = ["-v"]\nconnect_timeout_s = 10\n')
        assert get_ssh_options() == ["-v", "-o", "ConnectTimeout=10"]

    def test_per_remote_overrides(self, config_dir):
        _w(config_dir, '[sync.ssh]\noptions = ["-v"]\n\n[sync.remotes.box.ssh]\noptions = ["-q"]\n')
        assert get_ssh_options("box") == ["-q"]

    @pytest.mark.parametrize("setup", [
        None, "[sync]\n", '[sync.ssh]\nconnect_timeout_s = "bad"\n',
        'sync = "x"\n', '[sync]\nssh = "x"\n',
    ])
    def test_error_paths(self, config_dir, setup):
        if setup is not None:
            _w(config_dir, setup)
        assert get_ssh_options() == []


class TestCLIConfigIntegration:
    _Q = {"limit": 10, "chars": 200, "tool_chars": 120}

    def test_query_priority(self, config_dir):
        _w(config_dir, "[query]\nlimit = 25\nchars = 300\ntool_chars = 80\n")
        args = argparse.Namespace(limit=None, chars=None, tool_chars=None)
        apply_config_defaults(args, get_query_defaults, self._Q)
        assert (args.limit, args.chars, args.tool_chars) == (25, 300, 80)
        args = argparse.Namespace(limit=5, chars=None, tool_chars=None)
        apply_config_defaults(args, get_query_defaults, self._Q)
        assert args.limit == 5  # CLI wins
        _w(config_dir, "[search]\n")
        args = argparse.Namespace(limit=None, chars=None, tool_chars=None)
        apply_config_defaults(args, get_query_defaults, self._Q)
        assert (args.limit, args.chars, args.tool_chars) == (10, 200, 120)  # fallback

    def test_tools(self, config_dir):
        _w(config_dir, "[tools]\nlimit = 50\n")
        args = argparse.Namespace(limit=None)
        apply_config_defaults(args, get_tools_defaults, {"limit": 20})
        assert args.limit == 50
        args = argparse.Namespace(limit=10)
        apply_config_defaults(args, get_tools_defaults, {"limit": 20})
        assert args.limit == 10

    def _sa(self, **kw):
        base = dict(format=None, json=False, verbose=False, full=False,
                    thread=False, context=None, conversations=False)
        return argparse.Namespace(**{**base, **kw})

    @pytest.mark.parametrize("kw,expected", [
        ({}, "verbose"), ({"json": True}, None), ({"format": "json"}, "json"),
    ])
    def test_search_formatter(self, config_dir, kw, expected):
        _w(config_dir, '[search]\nformatter = "verbose"\n')
        args = self._sa(**kw)
        apply_config_defaults(args, get_search_defaults, skip_if=_has_explicit_formatter)
        assert args.format == expected
