"""Structural guards for the reference-doc generator (scripts/gen_docs.py).

`./dev docs --check` verifies *freshness* (committed == regenerated) but not
*correctness*: when the generator's subcommand extraction silently started
returning [], the check happily certified the gutted cli.md stub as current.
These tests assert the generator emits a section for every code-owned entity —
subcommand, config key, table — using the code structures themselves as
independent oracles, so a generator that stops emitting sections fails here
even though its output is internally "fresh".
"""

from __future__ import annotations

import argparse
import importlib.util
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_GEN_PATH = _REPO / "scripts" / "gen_docs.py"


@pytest.fixture(scope="module")
def gen():
    """Load scripts/gen_docs.py as a module (it isn't an importable package)."""
    spec = importlib.util.spec_from_file_location("gen_docs", _GEN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _subcommands() -> list[str]:
    # Oracle: the real argparse tree, independent of gen_docs' own extraction.
    from siftd.cli import _build_parser

    parser = _build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices.keys())
    return []


def test_cli_docs_have_a_section_per_subcommand(gen):
    """Regression guard: cli.md must document every registered subcommand.

    The brand redesign dropped the `{cmd,...}` usage block the old extractor
    scraped, leaving cli.md a one-section stub. Assert against the parser tree.
    """
    subcommands = _subcommands()
    assert subcommands, "oracle broken: no subcommands found on the parser"
    cli_md = gen.generate_cli_docs()
    missing = [c for c in subcommands if f"## siftd {c}" not in cli_md]
    assert not missing, f"cli.md is missing per-command sections for: {missing}"


def test_config_docs_cover_every_schema_key(gen):
    from siftd.config import _CONFIG_SCHEMA

    config_md = gen.generate_config_docs()
    missing = [
        e.pattern
        for e in _CONFIG_SCHEMA
        if f"`{e.pattern.split('.')[-1]}`" not in config_md
    ]
    assert not missing, f"config.md is missing keys for: {missing}"


def test_schema_docs_cover_every_table(gen):
    schema_sql = (_REPO / "src" / "siftd" / "storage" / "schema.sql").read_text()
    # Oracle: tables declared in the SQL, extracted independently of gen_docs'
    # parser (so a parser that silently drops a table is also caught).
    tables = re.findall(
        r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`]?(\w+)",
        schema_sql,
        re.IGNORECASE,
    )
    assert tables, "oracle broken: no tables parsed from schema.sql"
    schema_md = gen.generate_schema_docs()
    missing = [t for t in tables if f"### {t}" not in schema_md]
    assert not missing, f"schema.md is missing sections for tables: {missing}"
