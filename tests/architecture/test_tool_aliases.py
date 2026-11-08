"""Raw tool names reach canonical `category.action` forms, or are listed as not doing so.

`src/siftd/adapters/README.md` states the invariant in prose: raw tool names
must be mapped to canonical forms via `TOOL_ALIASES` so cross-tool queries
work. Nothing connected that sentence to the adapters, and it drifted two ways:

- pi_agent, opencode and copilot_cli shipped `TOOL_ALIASES = {}`, so their
  `bash` / `read` / `edit` sat beside every other harness's `shell.execute` /
  `file.read` / `file.edit`, and a per-tool count across harnesses split in two.
- An alias whose target isn't seeded in `CANONICAL_TOOLS` is skipped by
  `ensure_tool_aliases` without a word. Claude Code's `BashOutput`,
  `ToolSearch`, `SendMessage`, `Workflow` and `Exit/EnterPlanMode`, and Codex's
  `wait`, were declared and stored raw anyway.

Three properties, each enumerable:

1. Every built-in alias targets a seeded canonical tool (so none is skipped).
2. Every raw tool name a built-in adapter's golden fixtures produce is aliased,
   or listed in RAW_BY_DECISION with why. The list is shrink-only: an entry the
   fixtures no longer produce, or that has since been aliased, fails until
   removed, so it can't go stale into a permission slip.
3. Aliases are keyed by the raw name exactly as the adapter writes it.

The base is golden fixtures, so a tool name no fixture exercises is invisible
here, as it is to every fixture-based check. That is the scope of the claim.
"""

from __future__ import annotations

import json

import pytest
from architecture.support import REPO_ROOT

from siftd.adapters.registry import load_builtin_adapters
from siftd.storage.sqlite import CANONICAL_TOOLS

GOLDEN = REPO_ROOT / "tests" / "fixtures" / "adapters"
CANONICAL = frozenset(t["name"] for t in CANONICAL_TOOLS)

# adapter -> {raw tool name: why it stays raw}. Shrink-only.
RAW_BY_DECISION: dict[str, dict[str, str]] = {
    "antigravity_cli": {
        "schedule": "no canonical scheduling tool yet",
        "send_message": "agent messaging; task.message fits Claude's SendMessage, unconfirmed here",
    },
    "codex_cli": {
        "send_message": "agent messaging; task.message fits Claude's SendMessage, unconfirmed here",
    },
    "copilot_cli": {
        "report_intent": "status narration, no canonical form",
        "task": "ambiguous between task.spawn and ui.todo in Copilot's logs",
    },
    "pi_agent": {
        "Read": "a capitalised name from a proxied Claude tool; not pi's own read",
    },
    "vscode": {
        "configure_python_environment": "environment setup, no canonical form",
    },
}


def _aliases() -> dict[str, dict[str, str]]:
    return {p.name: dict(getattr(p.module, "TOOL_ALIASES", None) or {}) for p in load_builtin_adapters()}


def _fixture_tool_names() -> dict[str, set[str]]:
    names: dict[str, set[str]] = {}

    def walk(x, adapter: str) -> None:
        if isinstance(x, dict):
            if isinstance(x.get("tool_name"), str):
                names.setdefault(adapter, set()).add(x["tool_name"])
            for v in x.values():
                walk(v, adapter)
        elif isinstance(x, list):
            for v in x:
                walk(v, adapter)

    for path in GOLDEN.glob("*/*/expected.json"):
        walk(json.loads(path.read_text()), path.parent.parent.name)
    return names


@pytest.mark.parametrize("adapter", sorted(_aliases()))
def test_every_alias_targets_a_seeded_canonical_tool(adapter):
    unseeded = sorted({t for t in _aliases()[adapter].values() if t not in CANONICAL})
    assert not unseeded, (
        f"{adapter}: TOOL_ALIASES targets {unseeded}, which CANONICAL_TOOLS doesn't seed; "
        "ensure_tool_aliases would skip them and store the raw names. Seed them in storage/sqlite.py."
    )


def test_every_fixture_tool_name_is_aliased_or_listed():
    aliases, seen = _aliases(), _fixture_tool_names()
    problems = []
    for adapter, raw_names in sorted(seen.items()):
        listed = RAW_BY_DECISION.get(adapter, {})
        for raw in sorted(raw_names):
            if raw not in aliases.get(adapter, {}) and raw not in listed:
                problems.append(f"{adapter}: {raw!r} is neither aliased nor in RAW_BY_DECISION")
    assert not problems, "\n".join(problems)


def test_raw_by_decision_is_shrink_only():
    aliases, seen = _aliases(), _fixture_tool_names()
    stale = []
    for adapter, listed in sorted(RAW_BY_DECISION.items()):
        for raw in sorted(listed):
            if raw in aliases.get(adapter, {}):
                stale.append(f"{adapter}: {raw!r} is aliased now; remove it from RAW_BY_DECISION")
            elif raw not in seen.get(adapter, set()):
                stale.append(f"{adapter}: {raw!r} isn't in any fixture; remove it from RAW_BY_DECISION")
    assert not stale, "\n".join(stale)
