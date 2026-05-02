import sqlite3

import pytest


def _create_minimal_content_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE events (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            conversation_id TEXT NOT NULL
        );
        CREATE TABLE event_content (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            block_index INTEGER NOT NULL,
            block_type TEXT NOT NULL,
            content TEXT NOT NULL
        );
        """
    )


def test_ensure_fts_table_upgrades_to_porter_and_rebuilds():
    from siftd.storage.fts import ensure_fts_table, rebuild_fts_index, search_content

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _create_minimal_content_tables(conn)

        # Old schema (no porter tokenizer)
        conn.execute(
            """
            CREATE VIRTUAL TABLE content_fts USING fts5(
                text_content,
                event_content_id UNINDEXED,
                event_id UNINDEXED,
                conversation_id UNINDEXED
            )
            """
        )

        conn.execute("INSERT INTO events (id, kind, conversation_id) VALUES (?, ?, ?)", ("ev1", "prompt", "c1"))
        conn.execute(
            "INSERT INTO event_content (id, event_id, block_index, block_type, content) VALUES (?, ?, ?, ?, ?)",
            ("ec1", "ev1", 0, "text", '{"text":"writing new files"}'),
        )
        conn.commit()

        rebuild_fts_index(conn)

        # Without stemming, "write" should not match "writing"
        assert search_content(conn, "write", limit=10) == []

        ensure_fts_table(conn)

        # After upgrade, "write" should match "writing"
        hits = search_content(conn, "write", limit=10)
        assert any(h["conversation_id"] == "c1" for h in hits)

        # Table SQL reflects porter tokenizer
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='content_fts'"
        ).fetchone()[0]
        assert "porter" in (sql or "").lower()
    finally:
        conn.close()


def test_fts5_recall_conversations_falls_back_to_or_when_and_too_small():
    from siftd.storage.fts import ensure_fts_table, fts5_recall_conversations, insert_fts_content

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_fts_table(conn)

        # 1 conversation matches AND; many match only one term.
        insert_fts_content(conn, "ec_and", "ev_and", "c_and", "token refresh")
        for i in range(6):
            insert_fts_content(conn, f"ec_t_{i}", f"ev_t_{i}", f"c_token_{i}", "token")
        for i in range(6):
            insert_fts_content(conn, f"ec_r_{i}", f"ev_r_{i}", f"c_refresh_{i}", "refresh")
        conn.commit()

        ids, mode = fts5_recall_conversations(conn, "token refresh", limit=80)
        assert mode == "or"
        assert len(ids) > 1
        assert "c_and" in ids
    finally:
        conn.close()


def test_fts5_recall_conversations_keeps_and_when_enough_hits():
    from siftd.storage.fts import ensure_fts_table, fts5_recall_conversations, insert_fts_content

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_fts_table(conn)

        for i in range(10):
            insert_fts_content(conn, f"ec_{i}", f"ev_{i}", f"c_{i}", "token refresh")
        conn.commit()

        ids, mode = fts5_recall_conversations(conn, "token refresh", limit=80)
        assert mode == "and"
        assert len(ids) == 10
    finally:
        conn.close()


def test_extract_tool_summary_chunks_smoke():
    from siftd.embeddings.chunker import extract_tool_summary_chunks

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE tools (id TEXT PRIMARY KEY, name TEXT, category TEXT);
            CREATE TABLE tool_calls (
                conversation_id TEXT NOT NULL,
                tool_id TEXT,
                input TEXT,
                status TEXT,
                timestamp INTEGER
            );
            """
        )
        conn.executemany(
            "INSERT INTO tools (id, name, category) VALUES (?, ?, ?)",
            [
                ("t_read", "file.read", "file"),
                ("t_shell", "shell.execute", "shell"),
                ("t_grep", "search.grep", "search"),
            ],
        )
        conn.executemany(
            "INSERT INTO tool_calls (conversation_id, tool_id, input, status, timestamp) VALUES (?, ?, ?, ?, ?)",
            [
                ("c1", "t_read", '{"file_path":"/repo/pyproject.toml"}', "success", 1),
                ("c1", "t_shell", '{"command":"git status","description":"Check working tree"}', "success", 2),
                ("c1", "t_grep", '{"pattern":"TODO"}', "success", 3),
                ("c1", "t_shell", '{"command":"git diff"}', "error", 4),
            ],
        )
        conn.commit()

        chunks = extract_tool_summary_chunks(conn, conversation_ids={"c1"})
        assert len(chunks) == 1
        c = chunks[0]
        assert c["conversation_id"] == "c1"
        assert c["chunk_type"] == "tool_summary"
        text = c["text"]
        assert "Tools used in this conversation" in text
        assert "- file.read: 1 calls" in text
        assert "- shell.execute: 2 calls" in text
        assert "- search.grep: 1 calls" in text
        assert "Files accessed: pyproject.toml" in text
        assert "Shell commands: git" in text
        assert "Shell descriptions: Check working tree" in text
        assert "Grep patterns: TODO" in text
        assert "Tool errors: 1" in text
    finally:
        conn.close()


def test_extract_tool_summary_chunks_raw_names_use_category():
    """Non-canonical tool names still produce hints when category is set."""
    from siftd.embeddings.chunker import extract_tool_summary_chunks

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE tools (id TEXT PRIMARY KEY, name TEXT, category TEXT);
            CREATE TABLE tool_calls (
                conversation_id TEXT NOT NULL,
                tool_id TEXT,
                input TEXT,
                status TEXT,
                timestamp INTEGER
            );
            """
        )
        # Raw names stored without canonical alias mapping
        conn.executemany(
            "INSERT INTO tools (id, name, category) VALUES (?, ?, ?)",
            [
                ("t_read", "read", "file"),
                ("t_bash", "Bash", "shell"),
                ("t_grep", "Grep", "search"),
            ],
        )
        conn.executemany(
            "INSERT INTO tool_calls (conversation_id, tool_id, input, status, timestamp) VALUES (?, ?, ?, ?, ?)",
            [
                ("c1", "t_read", '{"file_path":"/repo/pyproject.toml"}', "success", 1),
                ("c1", "t_bash", '{"command":"git status","description":"Check tree"}', "success", 2),
                ("c1", "t_grep", '{"pattern":"TODO"}', "success", 3),
            ],
        )
        conn.commit()

        chunks = extract_tool_summary_chunks(conn, conversation_ids={"c1"})
        assert len(chunks) == 1
        text = chunks[0]["text"]
        assert "Files accessed: pyproject.toml" in text
        assert "Shell commands: git" in text
        assert "Shell descriptions: Check tree" in text
        assert "Grep patterns: TODO" in text
    finally:
        conn.close()


def test_hybrid_search_fts5_passthrough_is_opt_in(tmp_path):
    pytest.importorskip("fastembed")

    from siftd.search import hybrid_search
    from siftd.storage.embeddings import open_embeddings_db
    from siftd.storage.fts import insert_fts_content
    from siftd.storage.sqlite import open_database

    db_path = tmp_path / "siftd.db"
    embed_db_path = tmp_path / "embeddings.db"

    # Create an empty embeddings DB (valid schema, but no chunks).
    embed_conn = open_embeddings_db(embed_db_path)
    embed_conn.close()

    # Create a main DB and seed FTS with enough matches to fill limit.
    conn = open_database(db_path, read_only=False)
    try:
        for i in range(3):
            insert_fts_content(conn, f"ec_{i}", f"ev_{i}", f"c_{i}", "needle haystack")
        conn.commit()
    finally:
        conn.close()

    # Default behavior: no passthrough, so empty embeddings index => no results.
    no_pass = hybrid_search(
        "needle",
        db_path=db_path,
        embed_db_path=embed_db_path,
        embeddings_only=False,
        exclude_active=False,
        include_derivative=True,
        n=3,
        recall=80,
        fts5_passthrough=False,
    )
    assert no_pass == []

    # Opt-in passthrough: return FTS5 snippets even with empty embeddings index.
    passed = hybrid_search(
        "needle",
        db_path=db_path,
        embed_db_path=embed_db_path,
        embeddings_only=False,
        exclude_active=False,
        include_derivative=True,
        n=3,
        recall=80,
        fts5_passthrough=True,
    )
    assert len(passed) == 3
    assert all(r.chunk_type == "fts5" for r in passed)

    # Passthrough only triggers when FTS5 can fill `n`.
    not_enough = hybrid_search(
        "needle",
        db_path=db_path,
        embed_db_path=embed_db_path,
        embeddings_only=False,
        exclude_active=False,
        include_derivative=True,
        n=4,
        recall=80,
        fts5_passthrough=True,
    )
    assert not_enough == []
