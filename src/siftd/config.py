"""User configuration management for siftd.

Config file location: ~/.config/siftd/config.toml

Example config:
    [ask]
    formatter = "verbose"
"""

import sys
from typing import cast

import tomlkit
import tomlkit.exceptions
from tomlkit import TOMLDocument
from tomlkit.container import Container

from siftd.paths import config_dir, config_file


def load_config() -> TOMLDocument:
    """Load config from file, returning empty document if missing or invalid."""
    path = config_file()
    if not path.exists():
        return tomlkit.document()

    try:
        return tomlkit.parse(path.read_text())
    except tomlkit.exceptions.TOMLKitError as e:
        print(f"Warning: Invalid config file {path}: {e}", file=sys.stderr)
        return tomlkit.document()


def get_config(key: str) -> str | None:
    """Get config value by dotted key path (e.g., 'ask.formatter').

    Returns None if key doesn't exist.
    """
    doc = load_config()
    parts = key.split(".")

    current = doc
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]

    # Return string representation for non-container values
    if isinstance(current, (dict, list)):
        return None
    return str(current) if current is not None else None


def set_config(key: str, value: str) -> None:
    """Set config value by dotted key path (e.g., 'ask.formatter').

    Creates intermediate tables as needed. Preserves existing comments and formatting.
    """
    path = config_file()

    # Load existing or create new
    if path.exists():
        try:
            doc = tomlkit.parse(path.read_text())
        except tomlkit.exceptions.TOMLKitError:
            doc = tomlkit.document()
    else:
        doc = tomlkit.document()

    parts = key.split(".")
    current = doc

    # Navigate/create intermediate tables
    for part in parts[:-1]:
        if part not in current:
            current[part] = tomlkit.table()
        current = current[part]

    # Set the final value
    cast(Container, current)[parts[-1]] = value

    # Ensure config directory exists and write
    config_dir().mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc))


def get_ask_defaults() -> dict:
    """Get default values for 'siftd ask' command from config.

    Returns dict with keys matching argparse attribute names.
    Only includes values that are set in config.
    """
    doc = load_config()
    defaults = {}

    ask_config = doc.get("ask", {})
    if isinstance(ask_config, dict):
        if "formatter" in ask_config:
            defaults["format"] = str(ask_config["formatter"])

    return defaults
