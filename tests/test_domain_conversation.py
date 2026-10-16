"""``Conversation`` keeps the positional constructor v1 adapters were written against.

``preamble`` was added during the adapter rethink. A new field placed among the
positional ones silently rebinds every positional argument after it: a v1
``Conversation(id, harness, time, prompts, "/workspace", "main")`` put the
workspace in ``preamble``. The field is keyword-only so that cannot recur.
"""

import inspect

from siftd.domain import Conversation, Harness, Response


def test_v1_positional_constructor_binds_workspace_and_branch():
    harness = Harness(name="claude_code")
    conv = Conversation("c1", harness, "2024-01-01T00:00:00Z", [], "/workspace", "main")
    assert conv.workspace_path == "/workspace"
    assert conv.branch == "main"
    assert conv.preamble == []


def test_preamble_is_keyword_only():
    assert inspect.signature(Conversation).parameters["preamble"].kind is inspect.Parameter.KEYWORD_ONLY


def test_preamble_keyword_is_accepted():
    harness = Harness(name="claude_code")
    response = Response(timestamp="T")
    conv = Conversation("c1", harness, "2024-01-01T00:00:00Z", preamble=[response])
    assert conv.preamble == [response]
    assert conv.turn_count == 1
