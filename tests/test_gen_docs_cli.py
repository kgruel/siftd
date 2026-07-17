"""Tests for the cli target of scripts/gen_docs.py (parser-introspection generator).

`docs/reference/cli.md` used to be built by regexing the classic argparse
`{a,b,c}` brace out of `siftd --help`. That broke silently — zero sections,
22-line file — when the CLI moved to the custom lanes help format (no brace to
find). The replacement enumerates commands via `public_commands()`, which reads
`siftd.cli._LANES` (the CLI's own lane registry) instead of parsing rendered
text. This test pins the contract: every public lane command must show up as
its own section in the generated content, so a future help-format change can
never again silently truncate the reference doc to nothing.

Stays in the base lane: `siftd.cli` and its `--help` rendering need no
optional deps.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GEN_DOCS = _REPO_ROOT / "scripts" / "gen_docs.py"


def _load_gen_docs():
    spec = importlib.util.spec_from_file_location("gen_docs", _GEN_DOCS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen_docs = _load_gen_docs()


def test_public_commands_matches_lane_registry():
    from siftd.cli import _LANES

    expected = [cmd for _lane, cmds in _LANES for cmd in cmds.split()]
    assert gen_docs.public_commands() == expected


def test_cli_docs_has_a_section_per_public_command():
    """Every public lane command gets its own `## siftd <cmd>` section.

    Guards against a future help-format change silently truncating the
    generator back to zero sections the way the brace-regex one did.
    """
    content = gen_docs.generate_cli_docs()
    assert "## siftd\n" in content
    for cmd in gen_docs.public_commands():
        assert f"## siftd {cmd}\n" in content, f"missing cli.md section for {cmd!r}"


def test_cli_docs_excludes_plumbing():
    from siftd.cli import _PLUMBING

    content = gen_docs.generate_cli_docs()
    for cmd in _PLUMBING:
        assert f"## siftd {cmd}\n" not in content, f"plumbing command leaked into cli.md: {cmd!r}"


@pytest.mark.skipif(
    sys.version_info[:2] != gen_docs.CANONICAL_PYTHON,
    reason="cli.md is byte-reproducible only under the canonical interpreter "
    "(argparse help rendering varies across versions); the ci.yml docs job "
    "enforces freshness there",
)
def test_committed_cli_md_is_up_to_date():
    """The checked-in docs/reference/cli.md must match the generator's output."""
    committed = (_REPO_ROOT / "docs" / "reference" / "cli.md").read_text()
    assert committed == gen_docs.generate_cli_docs(), "docs/reference/cli.md is stale — run ./dev docs"
