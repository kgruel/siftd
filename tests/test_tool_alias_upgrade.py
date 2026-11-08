"""A declared tool alias takes over from the self-alias an earlier ingest created.

Before an adapter declared ``bash -> shell.execute``, ingesting its ``bash`` calls ran
``get_or_create_tool_by_alias``, which creates a tool named ``bash`` and the alias
``(bash, harness) -> bash``. ``tool_aliases`` is unique on ``(raw_name, harness_id)``, so
``ensure_tool_aliases``'s ``INSERT OR IGNORE`` then dropped the declared alias: on any DB
that had already ingested the harness, new calls kept landing under ``bash`` and old ones
stayed there (182 of 358 shell sessions over two weeks on a real server were invisible to
``--tool shell.execute``). The declaration wins now, and that harness's stored calls follow.
"""

import pytest

import siftd.storage.sqlite as sq


@pytest.fixture
def conn(tmp_path):
    sq._tool_alias_cache.clear()
    sq._tool_name_cache.clear()
    c = sq.create_database(tmp_path / "t.db")
    yield c
    c.close()
    sq._tool_alias_cache.clear()
    sq._tool_name_cache.clear()


def _call(conn, harness, raw, n):
    ws = sq.get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    cid = sq.insert_conversation(conn, f"{harness}-{n}", harness, ws, "2024-01-01T00:00:00Z")
    pid = sq.insert_prompt(conn, cid, f"p{n}", "2024-01-01T00:00:00Z")
    rid = sq.insert_response(conn, cid, pid, None, None, f"r{n}", "2024-01-01T00:00:01Z", 1, 1)
    tool_id = sq.get_or_create_tool_by_alias(conn, raw, harness)
    return sq.insert_tool_call(conn, rid, cid, tool_id, f"t{n}", "{}", None, "success", "2024-01-01T00:00:02Z")


def _tool_of(conn, event_id):
    return conn.execute(
        "SELECT t.name FROM event_tool_call tc JOIN tools t ON t.id = tc.tool_id WHERE tc.event_id = ?", (event_id,)
    ).fetchone()[0]


def test_a_declared_alias_replaces_the_self_alias_and_moves_stored_calls(conn):
    pi = sq.get_or_create_harness(conn, "pi_agent")
    old = _call(conn, pi, "bash", 0)  # ingested before the declaration: lands under "bash"
    assert _tool_of(conn, old) == "bash"

    sq.ensure_tool_aliases(conn, pi, {"bash": "shell.execute"})
    new = _call(conn, pi, "bash", 1)

    assert _tool_of(conn, new) == "shell.execute", "a new call follows the declaration"
    assert _tool_of(conn, old) == "shell.execute", "a stored call follows it too"


def test_another_harness_keeps_its_own_mapping(conn):
    """The tools row ``bash`` is shared by name; only the declaring harness's calls move."""
    pi = sq.get_or_create_harness(conn, "pi_agent")
    other = sq.get_or_create_harness(conn, "other_harness")
    mine, theirs = _call(conn, pi, "bash", 0), _call(conn, other, "bash", 1)

    sq.ensure_tool_aliases(conn, pi, {"bash": "shell.execute"})

    assert _tool_of(conn, mine) == "shell.execute"
    assert _tool_of(conn, theirs) == "bash"


def test_ensuring_again_changes_nothing(conn):
    pi = sq.get_or_create_harness(conn, "pi_agent")
    call = _call(conn, pi, "bash", 0)
    sq.ensure_tool_aliases(conn, pi, {"bash": "shell.execute"})
    before = conn.execute("SELECT * FROM tool_aliases ORDER BY id").fetchall()

    sq.ensure_tool_aliases(conn, pi, {"bash": "shell.execute"})

    assert conn.execute("SELECT * FROM tool_aliases ORDER BY id").fetchall() == before
    assert _tool_of(conn, call) == "shell.execute"


def test_doctor_finds_the_stale_alias_and_the_fix_repairs_it(tmp_path):
    """A DB that only receives merges never ingests, so doctor is how it gets repaired."""
    from siftd.api.database import find_stale_tool_aliases, repair_tool_aliases
    from siftd.adapters.registry import load_builtin_adapters

    sq._tool_alias_cache.clear()
    conn = sq.create_database(tmp_path / "s.db")
    pi = sq.get_or_create_harness(conn, "pi_agent")
    call = _call(conn, pi, "bash", 0)
    conn.commit()

    [stale] = [s for s in find_stale_tool_aliases(conn, load_builtin_adapters()) if s["raw_name"] == "bash"]
    assert stale == {"harness": "pi_agent", "raw_name": "bash", "stored": "bash", "declared": "shell.execute", "calls": 1}

    aliases, calls = repair_tool_aliases(conn, commit=True)

    assert (aliases, calls) == (1, 1)
    assert _tool_of(conn, call) == "shell.execute"
    assert find_stale_tool_aliases(conn, load_builtin_adapters()) == []
    conn.close()
    sq._tool_alias_cache.clear()
