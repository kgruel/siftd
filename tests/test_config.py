"""Tests for config module."""

import argparse
import os
import stat

import pytest
import tomlkit

from siftd import config as cfg
from siftd.cli._common import apply_config_defaults
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
    get_ssh_options,
    get_sync_remote,
    get_sync_remotes,
    get_sync_timeouts,
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

    @pytest.mark.parametrize("key,typ", [("serve.host", "string"), (_L, "list[string]")])
    def test_match(self, key, typ):
        assert _match_schema(key).expected == typ

    def test_serve_defaults(self):
        assert _match_schema("serve.host").default == "127.0.0.1"
        assert _match_schema("serve.auth.delegation_token").expected == "string"

    def test_no_match(self):
        assert _match_schema("totally.unknown.deep.key") is None

    def test_iter_config_items(self):
        keys = [k for k, _ in _iter_config_items({"serve": {"host": "0.0.0.0"}})]
        assert "serve" in keys and "serve.host" in keys
        assert list(_iter_config_items("not a dict")) == []

    def test_validate_warns(self, capsys):
        doc = tomlkit.parse('unknown_key = "v"\n[query]\nlimit = "x"\n')
        _validate_config(doc)
        err = capsys.readouterr().err
        assert "Unknown config key" in err and "expects int" in err

    def test_validate_clean(self, capsys):
        _validate_config(tomlkit.parse('[serve]\nhost = "0.0.0.0"\n'))
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
        _w(config_dir, '[serve]\nhost = "localhost"\n')
        assert load_config()["serve"]["host"] == "localhost"
        _w(config_dir, "invalid [ toml")
        assert len(load_config()) == 0
        assert "Warning" in capsys.readouterr().err

    @pytest.mark.parametrize("key,expected", [
        ("serve.host", "localhost"), ("serve.nonexistent", None),
        ("nonexistent.key", None), ("serve", None), (_L, None),
    ])
    def test_get(self, config_dir, key, expected):
        _w(config_dir, '[adapters.claude_code]\nlocations = ["/a"]\n[serve]\nhost = "localhost"\n')
        assert get_config(key) == expected

    def test_set(self, config_dir):
        set_config("serve.host", "localhost")
        assert "localhost" in (config_dir / "config.toml").read_text()
        set_config("serve.host", "0.0.0.0")
        assert get_config("serve.host") == "0.0.0.0"

    def test_set_preserves_comments(self, config_dir):
        _w(config_dir, '# My config\n[serve]\nhost = "localhost"\n')
        set_config("query.limit", "20")
        content = (config_dir / "config.toml").read_text()
        assert "# My config" in content and "localhost" in content

    def test_set_rejects_unknown(self, config_dir):
        with pytest.raises(ValueError, match="Unknown config key"):
            set_config("search.limit", "20")

    def test_set_corrupt_recovery(self, config_dir):
        _w(config_dir, "invalid [ toml")
        set_config("serve.host", "localhost")
        assert get_config("serve.host") == "localhost"

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
        set_config("serve.host", "localhost")
        assert isinstance(load_config()["serve"]["host"], str)


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
        _w(config_dir, '[serve]\nhost = "localhost"\n')
        with pytest.raises(ValueError):
            fn("serve.host", "x")

    @pytest.mark.parametrize("setup", [None, "invalid [ toml", "[search]\n", "[adapters.claude_code]\n"])
    def test_remove_returns_false(self, config_dir, setup):
        if setup is not None:
            _w(config_dir, setup)
        assert remove_config_list(_L, "x") is False


class TestDefaultsAndLookups:
    def test_query_defaults(self, config_dir):
        assert get_query_defaults() == {}
        _w(config_dir, "[query]\nlimit = 25\nchars = 300\ntool_chars = 80\n")
        assert get_query_defaults() == {"limit": 25, "chars": 300, "tool_chars": 80}
        _w(config_dir, '[query]\nlimit = "abc"\nchars = 200\n')
        assert "limit" not in get_query_defaults() and get_query_defaults()["chars"] == 200

    def test_tag_prefixes_defaults(self, config_dir):
        """No config file → built-in defaults are returned."""
        from siftd.config import DEFAULT_TAG_PREFIXES, get_tag_prefixes

        prefixes = get_tag_prefixes()
        for name, value in DEFAULT_TAG_PREFIXES.items():
            assert prefixes[name] == value

    def test_tag_prefixes_user_extends(self, config_dir):
        """User entries are merged on top of defaults."""
        from siftd.config import get_tag_prefixes

        _w(config_dir, '[tag_prefixes]\nmyproj = "myproj:"\n')
        prefixes = get_tag_prefixes()
        assert prefixes["myproj"] == "myproj:"
        # Defaults still present
        assert prefixes["research"] == "research:"

    def test_tag_prefixes_user_overrides_default(self, config_dir):
        """A user entry with the same name overrides the default value."""
        from siftd.config import get_tag_prefixes

        _w(config_dir, '[tag_prefixes]\nresearch = "rsrch:"\n')
        assert get_tag_prefixes()["research"] == "rsrch:"

    def test_tag_prefixes_skips_non_string_values(self, config_dir):
        """Non-string values in the user table are silently skipped."""
        from siftd.config import DEFAULT_TAG_PREFIXES, get_tag_prefixes

        _w(config_dir, '[tag_prefixes]\nbroken = 123\nok = "ok:"\n')
        prefixes = get_tag_prefixes()
        assert "broken" not in prefixes
        assert prefixes["ok"] == "ok:"
        # Defaults retained
        assert prefixes["research"] == DEFAULT_TAG_PREFIXES["research"]

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
        None, "invalid [ toml", '[serve]\nhost = "localhost"\n', "[sync]\n",
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
        '[serve]\nhost = "localhost"\n', "[sync]\n", "invalid [ toml",
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


class TestSyncTimeouts:
    def test_hardcoded_defaults(self, config_dir):
        from siftd.config import get_sync_timeouts
        assert get_sync_timeouts() == (30, 600)

    def test_sync_global(self, config_dir):
        from siftd.config import get_sync_timeouts
        _w(config_dir, "[sync]\nconnect_timeout_s = 15\ncommand_timeout_s = 900\n")
        assert get_sync_timeouts() == (15, 900)

    def test_transport_overrides_global(self, config_dir):
        from siftd.config import get_sync_timeouts
        _w(config_dir, (
            "[sync]\nconnect_timeout_s = 15\ncommand_timeout_s = 900\n"
            "[sync.ssh]\nconnect_timeout_s = 20\ncommand_timeout_s = 1200\n"
        ))
        assert get_sync_timeouts(transport="ssh") == (20, 1200)
        # HTTP still uses sync global
        assert get_sync_timeouts(transport="http") == (15, 900)

    def test_per_remote_overrides_transport(self, config_dir):
        from siftd.config import get_sync_timeouts
        _w(config_dir, (
            "[sync.ssh]\nconnect_timeout_s = 20\ncommand_timeout_s = 300\n"
            "[sync.remotes.alcove]\ncommand_timeout_s = 900\n"
        ))
        # per-remote overrides transport for command, connect falls through
        assert get_sync_timeouts("alcove", "ssh") == (20, 900)

    def test_per_remote_transport_overrides_all(self, config_dir):
        from siftd.config import get_sync_timeouts
        _w(config_dir, (
            "[sync]\nconnect_timeout_s = 10\ncommand_timeout_s = 300\n"
            "[sync.ssh]\ncommand_timeout_s = 600\n"
            "[sync.remotes.alcove]\ncommand_timeout_s = 900\n"
            "[sync.remotes.alcove.ssh]\nconnect_timeout_s = 5\ncommand_timeout_s = 1800\n"
        ))
        assert get_sync_timeouts("alcove", "ssh") == (5, 1800)

    def test_partial_override(self, config_dir):
        from siftd.config import get_sync_timeouts
        _w(config_dir, "[sync.remotes.box.ssh]\ncommand_timeout_s = 120\n")
        # connect falls through all layers to hardcoded default
        assert get_sync_timeouts("box", "ssh") == (30, 120)

    def test_bad_values_ignored(self, config_dir):
        from siftd.config import get_sync_timeouts
        _w(config_dir, '[sync]\nconnect_timeout_s = "bad"\ncommand_timeout_s = 100\n')
        # bad connect falls through to hardcoded, command resolves normally
        assert get_sync_timeouts() == (30, 100)

    def test_nonexistent_remote(self, config_dir):
        from siftd.config import get_sync_timeouts
        _w(config_dir, "[sync.ssh]\ncommand_timeout_s = 400\n")
        assert get_sync_timeouts("nonexistent", "ssh") == (30, 400)


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

class TestConfigPermissions:
    def test_set_config_creates_file_with_0600(self, config_dir):
        set_config("serve.host", "localhost")
        path = config_dir / "config.toml"
        assert path.exists()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_set_config_creates_dir_with_0700(self, config_dir):
        set_config("serve.host", "localhost")
        mode = stat.S_IMODE(config_dir.stat().st_mode)
        assert mode == 0o700

    def test_append_config_list_enforces_permissions(self, config_dir):
        append_config_list(_L, "/a")
        path = config_dir / "config.toml"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_sync_remote_write_enforces_permissions(self, config_dir):
        set_sync_remote("box", "box.local", "/data/db")
        path = config_dir / "config.toml"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_existing_file_gets_permissions_tightened(self, config_dir):
        """A config file written by an older siftd version gets hardened on next write."""
        _w(config_dir, '[serve]\nhost = "old"\n')
        path = config_dir / "config.toml"
        path.chmod(0o644)  # simulate lax permissions
        set_config("serve.host", "new")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
