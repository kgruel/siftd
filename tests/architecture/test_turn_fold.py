"""One fold from events to turns; the places that ask "which events are prompts" are enumerated.

A turn is an optional prompt plus the responses (and their tool calls) that
follow it. The read side used to define that about seven times: the transcript
builder walked prompts and skipped responses with no parent, the exchange
fetchers keyed text by prompt, search positions counted prompts and walked
backwards by timestamp, the exchange-index tag resolver counted prompts, the
embedding chunker took its exchanges from the fetchers, and each disagreed
about what a response before the first prompt belongs to. The audit found a
forked sub-agent's 21 stored responses rendered as an empty transcript by
``show``, ``export`` and the web folio, while usage counted them.

So the fold is written once (``storage/turns.py``: ``fold_turns``, with
``load_turns`` and the ordinal helpers over it) and this is the CLAUDE.md
"enumerable property with a shrink-only allowlist". Two properties:

1. **A SQL literal selecting ``kind = 'prompt'`` is a place that can start a
   turn definition of its own.** Each survivor below says why it is not one: it
   counts prompt events, reads the prompt list the fold is handed, or backfills
   a legacy table. A new site fails here until someone writes down which it is.
2. **``fold_turns`` is called from three sites**, each with a reason it takes
   rows rather than a conversation id. A fourth caller is a fourth way to build
   turns and should go through ``load_turns``.

Scope and limits. Both match source text: a prompt selection spelled through a
helper that builds the predicate (``kind = ?`` with a bound ``'prompt'``) is
invisible to (1), and a Python-side regrouping of events that never calls
``fold_turns`` is invisible to (2). Neither can say whether an allowlisted site
*stays* innocent, only that its reason is written where a reviewer reads it.
Hand-written SQL shipped as data (``builtin_queries/*.sql``) is not swept.

Both allowlists are shrink-only.
"""

import re

from architecture.support import SRC, call_to, literal_matching, sites_by_function

PROMPT_KIND = re.compile(r"kind\s*=\s*'prompt'", re.IGNORECASE)

# (module, function) -> why a literal 'prompt' selection there is not a turn definition. Shrink-only.
PROMPT_SELECTIONS: dict[tuple[str, str], str] = {
    ("api/conversations.py", "_list_conversations_impl"): (
        "Counts prompt events for the listing's prompt_count; the turns column adds the preamble "
        "turn through _HAS_PREAMBLE_SQL, the SQL form of the fold's one preamble rule."
    ),
    ("api/search.py", "enumerate_tagged"): (
        "The first prompt's text as a hit's excerpt; it names no turn."
    ),
    ("storage/conversation_stats.py", "rebuild_conversation_stats"): (
        "Materializes prompt_count, a count of prompt events (not a usage fact, not a turn count)."
    ),
    ("storage/events.py", "get_prompts_for_conv"): (
        "The prompt-list primitive: prompt rows in recorded order, which callers hand to the fold."
    ),
    ("storage/queries.py", "fetch_exchanges"): (
        "Selects the prompts whose turns are wanted and hands them, with the responses, to fold_turns."
    ),
    ("storage/queries.py", "fetch_prompt_timestamps"): "Timestamp lookup for given prompt ids.",
    ("storage/queries.py", "fetch_prompts_for_conversation"): (
        "get_conversation's prompt rows, which it hands to fold_turns."
    ),
    ("storage/queries.py", "fetch_table_count"): "Counts prompt events for the stats surface.",
    ("storage/sqlite.py", "_migrate_v4_polymorphic_schema"): "Legacy-table backfill during a v4 migration.",
    ("storage/turns.py", "conversation_ids_with_turns"): (
        "The fold's own SQL form: a conversation has a turn when it has a prompt or a preamble response."
    ),
}

# (module, function) -> why it calls fold_turns directly. Shrink-only.
FOLD_CALLERS: dict[tuple[str, str], str] = {
    ("storage/turns.py", "load_turns"): "The one loader: a conversation's events in recorded order, folded.",
    ("storage/queries.py", "fetch_exchanges"): (
        "Folds the rows of many conversations (or of selected prompts) it has already fetched with "
        "their text, which load_turns' one-conversation shape cannot serve."
    ),
    ("api/conversations.py", "get_conversation"): (
        "Folds the prompt, response and tool-call rows it fetched with the columns the transcript needs."
    ),
}


def test_prompt_selections_are_all_accounted_for():
    sites = sites_by_function(literal_matching(PROMPT_KIND, "prompt-selection"))
    offenders = sorted(set(sites) - set(PROMPT_SELECTIONS))
    assert not offenders, (
        f"SQL selects kind = 'prompt' in {offenders}. A turn is a prompt plus what follows it, and "
        "`siftd.storage.turns` is the one place that decides what follows: read turns through "
        "`load_turns`/`fold_turns`. If this site counts or lists prompt events and truly is not a "
        "turn definition, write why in PROMPT_SELECTIONS."
    )


def test_every_listed_prompt_selection_still_exists():
    """The ratchet's own guard: an entry that no longer matches is a claim nothing tests."""
    sites = sites_by_function(literal_matching(PROMPT_KIND, "prompt-selection"))
    stale = sorted(set(PROMPT_SELECTIONS) - set(sites))
    assert not stale, f"drop {stale} from PROMPT_SELECTIONS: it no longer selects kind = 'prompt'"


# The preamble predicate, respelled with or without an alias. A respelling in any
# file is a second definition of "a response with no parent prompt".
PREAMBLE_PREDICATE = re.compile(r"kind\s*=\s*'response'\s+AND\s+(?:\w+\.)?parent_id\s+IS\s+NULL", re.IGNORECASE)


def test_the_preamble_predicate_has_one_spelling():
    """The preamble rule is written once, in storage/turns.py, and interpolated everywhere else (review F9)."""
    holders = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if PREAMBLE_PREDICATE.search(" ".join(path.read_text().split()))
    )
    assert holders == ["storage/turns.py"], (
        f"the preamble predicate is spelled in {holders}. Interpolate "
        "siftd.storage.turns.PREAMBLE_RESPONSE_SQL instead of respelling it."
    )


def test_fold_is_called_from_the_known_sites_only():
    sites = sites_by_function(call_to(("fold_turns",)))
    offenders = sorted(set(sites) - set(FOLD_CALLERS))
    assert not offenders, (
        f"fold_turns is called from {offenders}. Load a conversation's turns with "
        "`siftd.storage.turns.load_turns`; a caller that must fold rows it already holds "
        "needs its reason written in FOLD_CALLERS."
    )
    stale = sorted(set(FOLD_CALLERS) - set(sites))
    assert not stale, f"drop {stale} from FOLD_CALLERS: it no longer calls fold_turns"


def test_allowlists_only_shrink():
    assert set(PROMPT_SELECTIONS) == {
        ("api/conversations.py", "_list_conversations_impl"),
        ("api/search.py", "enumerate_tagged"),
        ("storage/conversation_stats.py", "rebuild_conversation_stats"),
        ("storage/events.py", "get_prompts_for_conv"),
        ("storage/queries.py", "fetch_exchanges"),
        ("storage/queries.py", "fetch_prompt_timestamps"),
        ("storage/queries.py", "fetch_prompts_for_conversation"),
        ("storage/queries.py", "fetch_table_count"),
        ("storage/sqlite.py", "_migrate_v4_polymorphic_schema"),
        ("storage/turns.py", "conversation_ids_with_turns"),
    }, "PROMPT_SELECTIONS is shrink-only: a new site needs its reason written here, where a reviewer reads it."
    assert set(FOLD_CALLERS) == {
        ("storage/turns.py", "load_turns"),
        ("storage/queries.py", "fetch_exchanges"),
        ("api/conversations.py", "get_conversation"),
    }, "FOLD_CALLERS is shrink-only: a new caller needs its reason written here."
