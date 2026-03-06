"""Tests for config module."""

import argparse

import pytest


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Set up a temporary config directory."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "siftd"


class TestLoadConfig:
    def test_missing_file_returns_empty(self, config_dir):
        from siftd.config import load_config

        doc = load_config()
        assert len(doc) == 0

    def test_valid_config_loads(self, config_dir):
        from siftd.config import load_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[search]\nformatter = "verbose"\n')

        doc = load_config()
        assert doc["search"]["formatter"] == "verbose"

    def test_invalid_toml_returns_empty(self, config_dir, capsys):
        from siftd.config import load_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text("invalid [ toml")

        doc = load_config()
        assert len(doc) == 0

        captured = capsys.readouterr()
        assert "Warning" in captured.err


class TestGetConfig:
    def test_get_existing_key(self, config_dir):
        from siftd.config import get_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[search]\nformatter = "json"\n')

        assert get_config("search.formatter") == "json"

    def test_get_missing_key(self, config_dir):
        from siftd.config import get_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[search]\nformatter = "json"\n')

        assert get_config("search.nonexistent") is None
        assert get_config("nonexistent.key") is None

    def test_get_table_returns_none(self, config_dir):
        from siftd.config import get_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[search]\nformatter = "json"\n')

        # Getting a table itself should return None (not a scalar value)
        assert get_config("search") is None


class TestSetConfig:
    def test_set_creates_file(self, config_dir):
        from siftd.config import set_config

        set_config("search.formatter", "verbose")

        content = (config_dir / "config.toml").read_text()
        assert "verbose" in content

    def test_set_preserves_existing(self, config_dir):
        from siftd.config import set_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('# My config\n[search]\nformatter = "json"\n')

        set_config("query.limit", "20")

        content = (config_dir / "config.toml").read_text()
        # Original comment and value should be preserved
        assert "# My config" in content
        assert "json" in content
        assert "20" in content

    def test_set_updates_existing_key(self, config_dir):
        from siftd.config import get_config, set_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[search]\nformatter = "json"\n')

        set_config("search.formatter", "verbose")

        assert get_config("search.formatter") == "verbose"

    def test_set_rejects_unknown_key(self, config_dir):
        from siftd.config import set_config

        with pytest.raises(ValueError, match="Unknown config key"):
            set_config("search.limit", "20")


class TestConfigListOps:
    def test_append_creates_list(self, config_dir):
        from siftd.config import append_config_list, load_config

        changed = append_config_list(
            "adapters.claude_code.locations", "~/.claude/projects"
        )

        assert changed is True
        doc = load_config()
        assert doc["adapters"]["claude_code"]["locations"] == ["~/.claude/projects"]

    def test_append_dedup(self, config_dir):
        from siftd.config import append_config_list, load_config

        append_config_list("adapters.claude_code.locations", "~/.claude/projects")
        changed = append_config_list(
            "adapters.claude_code.locations", "~/.claude/projects"
        )

        assert changed is False
        doc = load_config()
        assert doc["adapters"]["claude_code"]["locations"] == ["~/.claude/projects"]

    def test_append_non_list_raises(self, config_dir):
        from siftd.config import append_config_list

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[search]\nformatter = "json"\n')

        with pytest.raises(ValueError):
            append_config_list("search.formatter", "verbose")

    def test_remove_value(self, config_dir):
        from siftd.config import load_config, remove_config_list

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text(
            '[adapters.claude_code]\nlocations = ["a", "b", "a"]\n'
        )

        changed = remove_config_list("adapters.claude_code.locations", "a")

        assert changed is True
        doc = load_config()
        assert doc["adapters"]["claude_code"]["locations"] == ["b"]

    def test_remove_leaves_empty_list(self, config_dir):
        from siftd.config import load_config, remove_config_list

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text(
            '[adapters.claude_code]\nlocations = ["a"]\n'
        )

        changed = remove_config_list("adapters.claude_code.locations", "a")

        assert changed is True
        doc = load_config()
        assert doc["adapters"]["claude_code"]["locations"] == []

    def test_remove_missing_value(self, config_dir):
        from siftd.config import load_config, remove_config_list

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text(
            '[adapters.claude_code]\nlocations = ["a"]\n'
        )

        changed = remove_config_list("adapters.claude_code.locations", "b")

        assert changed is False
        doc = load_config()
        assert doc["adapters"]["claude_code"]["locations"] == ["a"]

    def test_remove_non_list_raises(self, config_dir):
        from siftd.config import remove_config_list

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[search]\nformatter = "json"\n')

        with pytest.raises(ValueError):
            remove_config_list("search.formatter", "json")

class TestGetSearchDefaults:
    def test_returns_formatter_as_format(self, config_dir):
        from siftd.config import get_search_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[search]\nformatter = "thread"\n')

        defaults = get_search_defaults()
        # 'formatter' in config maps to 'format' arg
        assert defaults == {"format": "thread"}

    def test_empty_when_no_config(self, config_dir):
        from siftd.config import get_search_defaults

        defaults = get_search_defaults()
        assert defaults == {}


class TestCoerceValue:
    def test_true_string(self):
        from siftd.config import _coerce_value

        assert _coerce_value("true") is True
        assert _coerce_value("True") is True
        assert _coerce_value("TRUE") is True

    def test_false_string(self):
        from siftd.config import _coerce_value

        assert _coerce_value("false") is False
        assert _coerce_value("False") is False
        assert _coerce_value("FALSE") is False

    def test_string_passthrough(self):
        from siftd.config import _coerce_value

        assert _coerce_value("verbose") == "verbose"
        assert _coerce_value("20") == "20"
        assert _coerce_value("/tmp/test.db") == "/tmp/test.db"
        assert _coerce_value("") == ""

    def test_truthy_strings_not_coerced(self):
        from siftd.config import _coerce_value

        # Only exact "true"/"false" are coerced
        assert _coerce_value("yes") == "yes"
        assert _coerce_value("no") == "no"
        assert _coerce_value("1") == "1"
        assert _coerce_value("0") == "0"


class TestSetConfigCoercion:
    def test_set_false_stores_bool(self, config_dir):
        from siftd.config import load_config, set_config

        set_config("ingestion.filter_binary", "false")

        doc = load_config()
        val = doc["ingestion"]["filter_binary"]
        assert val is False
        assert isinstance(val, bool)

    def test_set_true_stores_bool(self, config_dir):
        from siftd.config import load_config, set_config

        set_config("ingestion.filter_binary", "true")

        doc = load_config()
        val = doc["ingestion"]["filter_binary"]
        assert val is True
        assert isinstance(val, bool)

    def test_set_string_stays_string(self, config_dir):
        from siftd.config import load_config, set_config

        set_config("search.formatter", "verbose")

        doc = load_config()
        val = doc["search"]["formatter"]
        assert val == "verbose"
        assert isinstance(val, str)


class TestGetQueryDefaults:
    def test_configured_values(self, config_dir):
        from siftd.config import get_query_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text("[query]\nlimit = 25\nchars = 300\ntool_chars = 80\n")

        defaults = get_query_defaults()
        assert defaults == {"limit": 25, "chars": 300, "tool_chars": 80}

    def test_empty_config(self, config_dir):
        from siftd.config import get_query_defaults

        defaults = get_query_defaults()
        assert defaults == {}

    def test_non_int_rejected(self, config_dir):
        from siftd.config import get_query_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[query]\nlimit = "abc"\nchars = 200\n')

        defaults = get_query_defaults()
        # "abc" is not a valid int, should be skipped
        assert "limit" not in defaults
        assert defaults["chars"] == 200

    def test_partial_config(self, config_dir):
        from siftd.config import get_query_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text("[query]\nlimit = 5\n")

        defaults = get_query_defaults()
        assert defaults == {"limit": 5}


class TestGetAdapterLocations:
    def test_configured(self, config_dir):
        from siftd.config import get_adapter_locations

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text(
            '[adapters.claude_code]\nlocations = ["~/.claude/projects", "/other/path"]\n'
        )

        result = get_adapter_locations("claude_code")
        assert result == ["~/.claude/projects", "/other/path"]

    def test_unconfigured(self, config_dir):
        from siftd.config import get_adapter_locations

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text("[search]\n")

        assert get_adapter_locations("claude_code") is None

    def test_unknown_adapter(self, config_dir):
        from siftd.config import get_adapter_locations

        assert get_adapter_locations("nonexistent") is None

    def test_empty_adapters_section(self, config_dir):
        from siftd.config import get_adapter_locations

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text("[adapters]\n")

        assert get_adapter_locations("claude_code") is None


class TestDbPathConfig:
    def test_default_path(self, config_dir, tmp_path, monkeypatch):
        from siftd.paths import db_path

        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

        result = db_path()
        assert result.name == "siftd.db"
        assert "data" in str(result)

    def test_override(self, config_dir):
        from siftd.paths import db_path

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[db]\npath = "/tmp/custom.db"\n')

        result = db_path()
        assert str(result) == "/tmp/custom.db"

    def test_tilde_expansion(self, config_dir):
        from siftd.paths import db_path

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[db]\npath = "~/my/siftd.db"\n')

        result = db_path()
        assert "~" not in str(result)
        assert str(result).endswith("my/siftd.db")


class TestApplyQueryConfig:
    def test_config_applied(self, config_dir):
        from siftd.cli_common import apply_config_defaults
        from siftd.config import get_query_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text("[query]\nlimit = 25\nchars = 300\ntool_chars = 80\n")

        args = argparse.Namespace(limit=None, chars=None, tool_chars=None)
        apply_config_defaults(args, get_query_defaults, {"limit": 10, "chars": 200, "tool_chars": 120})

        assert args.limit == 25
        assert args.chars == 300
        assert args.tool_chars == 80

    def test_cli_overrides_config(self, config_dir):
        from siftd.cli_common import apply_config_defaults
        from siftd.config import get_query_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text("[query]\nlimit = 25\n")

        args = argparse.Namespace(limit=5, chars=None, tool_chars=None)
        apply_config_defaults(args, get_query_defaults, {"limit": 10, "chars": 200, "tool_chars": 120})

        assert args.limit == 5  # CLI wins
        assert args.chars == 200  # hardcoded fallback
        assert args.tool_chars == 120  # hardcoded fallback

    def test_hardcoded_fallbacks(self, config_dir):
        from siftd.cli_common import apply_config_defaults
        from siftd.config import get_query_defaults

        # No config file at all
        args = argparse.Namespace(limit=None, chars=None, tool_chars=None)
        apply_config_defaults(args, get_query_defaults, {"limit": 10, "chars": 200, "tool_chars": 120})

        assert args.limit == 10
        assert args.chars == 200
        assert args.tool_chars == 120


class TestGetToolsDefaults:
    def test_configured_limit(self, config_dir):
        from siftd.config import get_tools_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text("[tools]\nlimit = 50\n")

        assert get_tools_defaults() == {"limit": 50}

    def test_empty_config(self, config_dir):
        from siftd.config import get_tools_defaults

        assert get_tools_defaults() == {}

    def test_non_int_rejected(self, config_dir):
        from siftd.config import get_tools_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[tools]\nlimit = "all"\n')

        assert get_tools_defaults() == {}


class TestApplyToolsConfig:
    def test_config_applied(self, config_dir):
        from siftd.cli_common import apply_config_defaults
        from siftd.config import get_tools_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text("[tools]\nlimit = 50\n")

        args = argparse.Namespace(limit=None)
        apply_config_defaults(args, get_tools_defaults, {"limit": 20})

        assert args.limit == 50

    def test_cli_overrides_config(self, config_dir):
        from siftd.cli_common import apply_config_defaults
        from siftd.config import get_tools_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text("[tools]\nlimit = 50\n")

        args = argparse.Namespace(limit=10)
        apply_config_defaults(args, get_tools_defaults, {"limit": 20})

        assert args.limit == 10

    def test_hardcoded_fallback(self, config_dir):
        from siftd.cli_common import apply_config_defaults
        from siftd.config import get_tools_defaults

        args = argparse.Namespace(limit=None)
        apply_config_defaults(args, get_tools_defaults, {"limit": 20})

        assert args.limit == 20


class TestApplySearchConfig:
    def test_applies_default_formatter(self, config_dir):
        from siftd.cli_common import apply_config_defaults
        from siftd.cli_search import _has_explicit_formatter
        from siftd.config import get_search_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[search]\nformatter = "verbose"\n')

        args = argparse.Namespace(
            format=None,
            json=False,
            verbose=False,
            full=False,
            thread=False,
            context=None,
            conversations=False,
        )

        apply_config_defaults(args, get_search_defaults, skip_if=_has_explicit_formatter)

        assert args.format == "verbose"

    def test_cli_flag_overrides_config(self, config_dir):
        from siftd.cli_common import apply_config_defaults
        from siftd.cli_search import _has_explicit_formatter
        from siftd.config import get_search_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[search]\nformatter = "verbose"\n')

        args = argparse.Namespace(
            format=None,
            json=True,  # Explicit --json flag
            verbose=False,
            full=False,
            thread=False,
            context=None,
            conversations=False,
        )

        apply_config_defaults(args, get_search_defaults, skip_if=_has_explicit_formatter)

        # Should NOT apply config because --json is set
        assert args.format is None

    def test_explicit_format_overrides_config(self, config_dir):
        from siftd.cli_common import apply_config_defaults
        from siftd.cli_search import _has_explicit_formatter
        from siftd.config import get_search_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[search]\nformatter = "verbose"\n')

        args = argparse.Namespace(
            format="json",  # Explicit --format json
            json=False,
            verbose=False,
            full=False,
            thread=False,
            context=None,
            conversations=False,
        )

        apply_config_defaults(args, get_search_defaults, skip_if=_has_explicit_formatter)

        # Should keep explicit format
        assert args.format == "json"
