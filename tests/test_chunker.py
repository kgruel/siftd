"""Smoke tests for token-aware chunking."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from siftd.embeddings import chunker as ch
from siftd.embeddings.chunker import chunk_text


@pytest.fixture(scope="module")
def tokenizer():
    return _FakeTokenizer()


def test_short_text_passthrough(tokenizer):
    """Text already under target_tokens passes through unchanged."""
    text = "Hello, this is a short sentence."
    result = chunk_text(text, tokenizer, target_tokens=256, max_tokens=512)
    assert result == [text]


def test_empty_text(tokenizer):
    """Empty/whitespace text returns empty list."""
    assert chunk_text("", tokenizer) == []
    assert chunk_text("   ", tokenizer) == []


def test_long_text_chunks_within_max(tokenizer):
    """All chunks from a >1000 token text are within max_tokens."""
    tokenizer.no_truncation()

    sentences = [f"Sentence number {i} contains several words for testing." for i in range(200)]
    text = " ".join(sentences)

    input_tokens = len(tokenizer.encode(text).ids) - 2
    assert input_tokens > 1000, f"Expected >1000 tokens, got {input_tokens}"

    max_tokens = 512
    chunks = chunk_text(text, tokenizer, target_tokens=256, max_tokens=max_tokens, overlap_tokens=25)

    assert len(chunks) > 1, f"Expected multiple chunks, got {len(chunks)}"

    for i, chunk in enumerate(chunks):
        token_count = len(tokenizer.encode(chunk).ids)
        assert token_count <= max_tokens, (
            f"Chunk {i} has {token_count} tokens (max {max_tokens}): {chunk[:80]}..."
        )


def test_overlap_exists(tokenizer):
    """Adjacent chunks share some content when overlap_tokens > 0."""
    sentences = [f"Unique sentence {i} with distinct content here." for i in range(100)]
    text = " ".join(sentences)

    chunks = chunk_text(text, tokenizer, target_tokens=100, max_tokens=200, overlap_tokens=25)

    found_overlap = False
    for i in range(len(chunks) - 1):
        words_end = chunks[i].split()[-5:]
        words_start = chunks[i + 1].split()[:20]
        for word in words_end:
            if word in " ".join(words_start):
                found_overlap = True
                break
        if found_overlap:
            break

    assert found_overlap, "Expected overlap between adjacent chunks"


class _FakeTokenizer:
    def no_truncation(self):
        return None

    def encode(self, text):
        n = len(str(text).split())
        return SimpleNamespace(ids=list(range(n + 2)))


def test_split_helpers_cover_long_sentence_path():
    tok = _FakeTokenizer()
    text = """one two three four five six seven eight nine ten eleven twelve"""
    chunks = ch._split_with_overlap(tok, text, target_tokens=4, max_tokens=5, overlap_tokens=1)
    assert chunks and all(isinstance(c, str) for c in chunks)
    assert ch._split_sentences("a. b\n\nc") == ["a.", "b", "c"]


def test_split_with_overlap_flush_and_overlap_parts():
    tok = _FakeTokenizer()
    text = "one two. three four. five six. seven eight."
    chunks = ch._split_with_overlap(tok, text, target_tokens=3, max_tokens=10, overlap_tokens=2)
    assert len(chunks) >= 2 and chunks[0]


def test_split_with_overlap_flushes_before_oversized_sentence():
    tok = _FakeTokenizer()
    text = "small one. this sentence is intentionally way too long for max tokens branch now."
    out = ch._split_with_overlap(tok, text, target_tokens=20, max_tokens=5, overlap_tokens=1)
    assert len(out) >= 2


def test_window_exchanges_handles_oversized_exchange():
    tok = _FakeTokenizer()
    exchanges = [
        {"text": "short one", "prompt_id": "p1"},
        {"text": "a b c d e f g h i j", "prompt_id": "p2"},
        {"text": "tail", "prompt_id": "p3"},
    ]
    out = ch._window_exchanges(exchanges, tok, target_tokens=4, max_tokens=5, overlap_tokens=1)
    assert out and any(ids == ["p2"] for _, _, ids in out)


def test_window_exchanges_target_overflow_flushes_current_window():
    tok = _FakeTokenizer()
    exchanges = [
        {"text": "one two", "prompt_id": "p1"},
        {"text": "three four", "prompt_id": "p2"},
        {"text": "five six", "prompt_id": "p3"},
    ]
    out = ch._window_exchanges(exchanges, tok, target_tokens=4, max_tokens=20, overlap_tokens=1)
    assert len(out) >= 2 and out[0][2] == ["p1", "p2"]


def test_extract_exchange_window_chunks_with_stubbed_loader(monkeypatch):
    tok = _FakeTokenizer()
    monkeypatch.setattr(
        ch,
        "_load_exchanges",
        lambda *_a, **_k: {"c1": [{"text": "hello world", "prompt_id": "p1"}]},
    )
    conn = sqlite3.connect(":memory:")
    try:
        out = ch.extract_exchange_window_chunks(conn, tok)
    finally:
        conn.close()
    assert out[0]["conversation_id"] == "c1" and out[0]["chunk_type"] == "exchange"


def test_load_exchanges_forwards_to_storage_query(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(
        ch,
        "fetch_conversation_exchanges",
        lambda _c, **k: {"x": [{"text": "t", "prompt_id": "p"}], "args": [k["conversation_id"], k["exclude_conversation_ids"]]},
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
    # Explicit malformed JSON by category to cover parser-exception branches
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

    c4 = next(c for c in all_chunks if c["conversation_id"] == "c4")
    file_line = next(line for line in c4["text"].splitlines() if line.startswith("Files accessed:"))
    assert len(file_line.split(",")) <= 20

    filtered = ch.extract_tool_summary_chunks(conn, conversation_ids={"c1"})
    assert len(filtered) == 1 and filtered[0]["conversation_id"] == "c1"

    conn.close()
