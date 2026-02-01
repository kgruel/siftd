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

        set_config("search.limit", "20")

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


class TestApplySearchConfig:
    def test_applies_default_formatter(self, config_dir):
        from siftd.cli_search import _apply_search_config

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

        _apply_search_config(args)

        assert args.format == "verbose"

    def test_cli_flag_overrides_config(self, config_dir):
        from siftd.cli_search import _apply_search_config

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

        _apply_search_config(args)

        # Should NOT apply config because --json is set
        assert args.format is None

    def test_explicit_format_overrides_config(self, config_dir):
        from siftd.cli_search import _apply_search_config

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

        _apply_search_config(args)

        # Should keep explicit format
        assert args.format == "json"
