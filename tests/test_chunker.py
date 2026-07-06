"""Tests for token-aware chunking (schema-v2: estimator-decoupled, widened source_ids)."""

import json
import sqlite3

import pytest

from siftd.embeddings import chunker as ch
from siftd.embeddings.chunker import chunk_text, estimate_tokens


def test_estimate_tokens_char_heuristic():
    """The estimator is chars/4 (ceil), independent of any tokenizer."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2  # ceil(5/4)
    assert estimate_tokens("a" * 400) == 100


def test_short_text_passthrough():
    """Text already under target_tokens passes through unchanged."""
    text = "Hello, this is a short sentence."
    assert chunk_text(text, target_tokens=256, max_tokens=512) == [text]


def test_empty_text():
    """Empty/whitespace text returns empty list."""
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_long_text_chunks_within_max():
    """All chunks from a long text stay within max estimator-tokens."""
    sentences = [f"Sentence number {i} contains several words for testing purposes." for i in range(200)]
    text = " ".join(sentences)
    assert estimate_tokens(text) > 1000

    max_tokens = 384
    chunks = chunk_text(text, target_tokens=256, max_tokens=max_tokens, overlap_tokens=25)

    assert len(chunks) > 1
    for i, chunk in enumerate(chunks):
        assert estimate_tokens(chunk) <= max_tokens, (
            f"Chunk {i} has {estimate_tokens(chunk)} est-tokens (max {max_tokens})"
        )


def test_overlap_exists():
    """Adjacent chunks share some content when overlap_tokens > 0."""
    sentences = [f"Unique sentence {i} with distinct content here." for i in range(100)]
    text = " ".join(sentences)
    chunks = chunk_text(text, target_tokens=100, max_tokens=200, overlap_tokens=25)

    found_overlap = False
    for i in range(len(chunks) - 1):
        words_end = chunks[i].split()[-5:]
        words_start = chunks[i + 1].split()[:20]
        if any(word in " ".join(words_start) for word in words_end):
            found_overlap = True
            break
    assert found_overlap, "Expected overlap between adjacent chunks"


def test_split_helpers_cover_long_sentence_path():
    text = "one two three four five six seven eight nine ten eleven twelve"
    chunks = ch._split_with_overlap(text, target_tokens=4, max_tokens=5, overlap_tokens=1)
    assert chunks and all(isinstance(c, str) for c in chunks)
    assert ch._split_sentences("a. b\n\nc") == ["a.", "b", "c"]


def test_split_with_overlap_flush_and_overlap_parts():
    text = "one two. three four. five six. seven eight."
    chunks = ch._split_with_overlap(text, target_tokens=3, max_tokens=10, overlap_tokens=2)
    assert len(chunks) >= 2 and chunks[0]


def test_split_with_overlap_flushes_before_oversized_sentence():
    text = "small one. this sentence is intentionally way too long for the max tokens branch now."
    out = ch._split_with_overlap(text, target_tokens=20, max_tokens=2, overlap_tokens=1)
    assert len(out) >= 2


def test_window_exchanges_widens_source_ids_to_responses():
    """A window's source_ids carry prompt id first, then response ids (the RRF bridge)."""
    exchanges = [
        {"text": "short one", "prompt_id": "p1", "event_ids": ["p1", "r1a", "r1b"]},
        {"text": "short two", "prompt_id": "p2", "event_ids": ["p2", "r2"]},
    ]
    out = ch._window_exchanges(exchanges, target_tokens=256, max_tokens=512, overlap_tokens=1)
    assert len(out) == 1
    _, _, ids = out[0]
    assert ids[0] == "p1"  # prompt-first anchor preserved
    assert "r1a" in ids and "r1b" in ids and "r2" in ids


def test_window_exchanges_handles_oversized_exchange():
    """An oversized exchange splits into sub-chunks that all carry its event ids."""
    long_text = " ".join(f"word{i}" for i in range(400))  # > max estimator tokens
    exchanges = [
        {"text": "short", "prompt_id": "p1", "event_ids": ["p1", "r1"]},
        {"text": long_text, "prompt_id": "p2", "event_ids": ["p2", "r2"]},
        {"text": "tail", "prompt_id": "p3", "event_ids": ["p3"]},
    ]
    out = ch._window_exchanges(exchanges, target_tokens=4, max_tokens=5, overlap_tokens=1)
    assert out
    # every sub-chunk from the oversized exchange references its constituent events
    assert any(ids == ["p2", "r2"] for _, _, ids in out)


def test_window_exchanges_target_overflow_flushes_current_window():
    exchanges = [
        {"text": "one two three four", "prompt_id": "p1", "event_ids": ["p1", "r1"]},
        {"text": "five six seven eight", "prompt_id": "p2", "event_ids": ["p2", "r2"]},
    ]
    out = ch._window_exchanges(exchanges, target_tokens=4, max_tokens=40, overlap_tokens=1)
    assert len(out) >= 2 and out[0][2] == ["p1", "r1"]


def test_window_exchanges_falls_back_to_prompt_id_without_event_ids():
    """Legacy exchange dicts (prompt_id only) still produce a one-element source_ids."""
    exchanges = [{"text": "hello world", "prompt_id": "p1"}]
    out = ch._window_exchanges(exchanges, target_tokens=256, max_tokens=512, overlap_tokens=1)
    assert out[0][2] == ["p1"]


def test_extract_exchange_window_chunks_with_stubbed_loader(monkeypatch):
    monkeypatch.setattr(
        ch,
        "_load_exchanges",
        lambda *_a, **_k: {"c1": [{"text": "hello world", "prompt_id": "p1", "event_ids": ["p1", "r1"]}]},
    )
    conn = sqlite3.connect(":memory:")
    try:
        out = ch.extract_exchange_window_chunks(conn)
    finally:
        conn.close()
    assert out[0]["conversation_id"] == "c1" and out[0]["chunk_type"] == "exchange"
    assert out[0]["source_ids"] == ["p1", "r1"]
    assert out[0]["token_count"] > 0


def test_load_exchanges_forwards_to_storage_query(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(
        ch,
        "fetch_conversation_exchanges",
        lambda _c, **k: {"x": [{"text": "t", "prompt_id": "p", "event_ids": ["p"]}], "args": [k["conversation_id"], k["exclude_conversation_ids"]]},
    )
    try:
        out = ch._load_exchanges(conn, {"c2"}, "c1")
    finally:
        conn.close()
    assert out["x"][0]["prompt_id"] == "p" and out["args"] == ["c1", {"c2"}]


def test_extract_tool_summary_chunks_branches_and_filters():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tools (id TEXT PRIMARY KEY, name TEXT, category TEXT)")
    conn.execute(
        "CREATE TABLE events"
        " (id TEXT PRIMARY KEY, kind TEXT NOT NULL, conversation_id TEXT NOT NULL,"
        " parent_id TEXT, external_id TEXT, timestamp TEXT)"
    )
    conn.execute(
        "CREATE TABLE event_tool_call"
        " (event_id TEXT PRIMARY KEY, tool_id TEXT, input TEXT, result_hash TEXT, status TEXT)"
    )
    conn.execute("INSERT INTO tools (id, name, category) VALUES ('t1', 'file.read', 'file')")
    conn.execute("INSERT INTO tools (id, name, category) VALUES ('t2', 'shell.execute', 'shell')")
    conn.execute("INSERT INTO tools (id, name, category) VALUES ('t3', 'search.grep', 'search')")

    def _ins(eid, conv, tool_id, input_json, status, ts):
        conn.execute(
            "INSERT INTO events (id, kind, conversation_id, timestamp) VALUES (?, 'tool_call', ?, ?)",
            (eid, conv, ts),
        )
        conn.execute(
            "INSERT INTO event_tool_call (event_id, tool_id, input, status) VALUES (?, ?, ?, ?)",
            (eid, tool_id, input_json, status),
        )

    _ins("e1", "c1", "t1", json.dumps({"file_path": "/tmp/pyproject.toml"}), "success", "1")
    _ins("e2", "c1", "t2", json.dumps({"command": "pytest -q", "description": "run tests"}), "error", "2")
    _ins("e3", "c1", "t3", json.dumps({"pattern": "TODO"}), "success", "3")
    _ins("e4", "c2", None, "{bad", "success", "4")
    _ins("e5", "c3", "t1", "{bad", "success", "5")
    _ins("e6", "c3", "t2", "{bad", "success", "6")
    _ins("e7", "c3", "t3", "{bad", "success", "7")
    for i in range(25):
        _ins(f"e-c4-{i}", "c4", "t1", json.dumps({"file_path": f"/tmp/path_{i}.txt"}), "success", f"f{i}")
    conn.commit()

    all_chunks = ch.extract_tool_summary_chunks(conn)
    c1 = next(c for c in all_chunks if c["conversation_id"] == "c1")
    assert "Files accessed" in c1["text"]
    assert "Shell commands" in c1["text"] and "Grep patterns" in c1["text"]
    assert "Tool errors" in c1["text"]
    # tool_summary chunks carry no source_ids (vector-only; no FTS bridge)
    assert c1["source_ids"] == []

    c4 = next(c for c in all_chunks if c["conversation_id"] == "c4")
    file_line = next(line for line in c4["text"].splitlines() if line.startswith("Files accessed:"))
    assert len(file_line.split(",")) <= 20

    filtered = ch.extract_tool_summary_chunks(conn, conversation_ids={"c1"})
    assert len(filtered) == 1 and filtered[0]["conversation_id"] == "c1"

    conn.close()


@pytest.mark.parametrize("bad", ["", "   "])
def test_chunk_text_empty_variants(bad):
    assert chunk_text(bad) == []
