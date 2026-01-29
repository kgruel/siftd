"""Tests for config module."""

import argparse
from pathlib import Path

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
        (config_dir / "config.toml").write_text('[ask]\nformatter = "verbose"\n')

        doc = load_config()
        assert doc["ask"]["formatter"] == "verbose"

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
        (config_dir / "config.toml").write_text('[ask]\nformatter = "json"\n')

        assert get_config("ask.formatter") == "json"

    def test_get_missing_key(self, config_dir):
        from siftd.config import get_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[ask]\nformatter = "json"\n')

        assert get_config("ask.nonexistent") is None
        assert get_config("nonexistent.key") is None

    def test_get_table_returns_none(self, config_dir):
        from siftd.config import get_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[ask]\nformatter = "json"\n')

        # Getting a table itself should return None (not a scalar value)
        assert get_config("ask") is None


class TestSetConfig:
    def test_set_creates_file(self, config_dir):
        from siftd.config import set_config

        set_config("ask.formatter", "verbose")

        content = (config_dir / "config.toml").read_text()
        assert "verbose" in content

    def test_set_preserves_existing(self, config_dir):
        from siftd.config import set_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('# My config\n[ask]\nformatter = "json"\n')

        set_config("ask.limit", "20")

        content = (config_dir / "config.toml").read_text()
        # Original comment and value should be preserved
        assert "# My config" in content
        assert "json" in content
        assert "20" in content

    def test_set_updates_existing_key(self, config_dir):
        from siftd.config import get_config, set_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[ask]\nformatter = "json"\n')

        set_config("ask.formatter", "verbose")

        assert get_config("ask.formatter") == "verbose"


class TestGetAskDefaults:
    def test_returns_formatter_as_format(self, config_dir):
        from siftd.config import get_ask_defaults

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[ask]\nformatter = "thread"\n')

        defaults = get_ask_defaults()
        # 'formatter' in config maps to 'format' arg
        assert defaults == {"format": "thread"}

    def test_empty_when_no_config(self, config_dir):
        from siftd.config import get_ask_defaults

        defaults = get_ask_defaults()
        assert defaults == {}


class TestApplyAskConfig:
    def test_applies_default_formatter(self, config_dir):
        from siftd.cli_ask import _apply_ask_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[ask]\nformatter = "verbose"\n')

        args = argparse.Namespace(
            format=None,
            json=False,
            verbose=False,
            full=False,
            thread=False,
            context=None,
            conversations=False,
        )

        _apply_ask_config(args)

        assert args.format == "verbose"

    def test_cli_flag_overrides_config(self, config_dir):
        from siftd.cli_ask import _apply_ask_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[ask]\nformatter = "verbose"\n')

        args = argparse.Namespace(
            format=None,
            json=True,  # Explicit --json flag
            verbose=False,
            full=False,
            thread=False,
            context=None,
            conversations=False,
        )

        _apply_ask_config(args)

        # Should NOT apply config because --json is set
        assert args.format is None

    def test_explicit_format_overrides_config(self, config_dir):
        from siftd.cli_ask import _apply_ask_config

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text('[ask]\nformatter = "verbose"\n')

        args = argparse.Namespace(
            format="json",  # Explicit --format json
            json=False,
            verbose=False,
            full=False,
            thread=False,
            context=None,
            conversations=False,
        )

        _apply_ask_config(args)

        # Should keep explicit format
        assert args.format == "json"
