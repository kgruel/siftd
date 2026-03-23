"""Smoke tests for token-aware chunking."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from siftd.embeddings import chunker as ch
from siftd.embeddings.chunker import chunk_text


@pytest.fixture(scope="module")
def tokenizer():
    TextEmbedding = pytest.importorskip("fastembed", exc_type=ImportError).TextEmbedding
    emb = TextEmbedding("BAAI/bge-small-en-v1.5")
    return emb.model.tokenizer


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
    def encode(self, text):
        n = len(str(text).split())
        return SimpleNamespace(ids=list(range(n + 2)))


def test_split_helpers_cover_long_sentence_path():
    tok = _FakeTokenizer()
    text = """one two three four five six seven eight nine ten eleven twelve"""
    chunks = ch._split_with_overlap(tok, text, target_tokens=4, max_tokens=5, overlap_tokens=1)
    assert chunks and all(isinstance(c, str) for c in chunks)
    assert ch._split_sentences("a. b\n\nc") == ["a.", "b", "c"]


def test_window_exchanges_handles_oversized_exchange():
    tok = _FakeTokenizer()
    exchanges = [
        {"text": "short one", "prompt_id": "p1"},
        {"text": "a b c d e f g h i j", "prompt_id": "p2"},
        {"text": "tail", "prompt_id": "p3"},
    ]
    out = ch._window_exchanges(exchanges, tok, target_tokens=4, max_tokens=5, overlap_tokens=1)
    assert out and any(ids == ["p2"] for _, _, ids in out)


def test_extract_exchange_window_chunks_with_stubbed_loader(monkeypatch):
    tok = _FakeTokenizer()
    monkeypatch.setattr(
        ch,
        "_load_exchanges",
        lambda *_a, **_k: {"c1": [{"text": "hello world", "prompt_id": "p1"}]},
    )
    out = ch.extract_exchange_window_chunks(sqlite3.connect(":memory:"), tok)
    assert out[0]["conversation_id"] == "c1" and out[0]["chunk_type"] == "exchange"


def test_extract_tool_summary_chunks_branches_and_filters():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tools (id TEXT PRIMARY KEY, name TEXT, category TEXT)")
    conn.execute(
        "CREATE TABLE tool_calls (conversation_id TEXT, tool_id TEXT, input TEXT, status TEXT, timestamp TEXT)"
    )
    conn.execute("INSERT INTO tools (id, name, category) VALUES ('t1', 'file.read', 'file')")
    conn.execute("INSERT INTO tools (id, name, category) VALUES ('t2', 'shell.execute', 'shell')")
    conn.execute("INSERT INTO tools (id, name, category) VALUES ('t3', 'search.grep', 'search')")
    conn.execute(
        "INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?)",
        ("c1", "t1", json.dumps({"file_path": "/tmp/pyproject.toml"}), "success", "1"),
    )
    conn.execute(
        "INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?)",
        ("c1", "t2", json.dumps({"command": "pytest -q", "description": "run tests"}), "error", "2"),
    )
    conn.execute(
        "INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?)",
        ("c1", "t3", json.dumps({"pattern": "TODO"}), "success", "3"),
    )
    conn.execute("INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?)", ("c2", None, "{bad", "success", "4"))
    conn.commit()

    all_chunks = ch.extract_tool_summary_chunks(conn)
    c1 = next(c for c in all_chunks if c["conversation_id"] == "c1")
    assert "Files accessed" in c1["text"]
    assert "Shell commands" in c1["text"] and "Grep patterns" in c1["text"]
    assert "Tool errors" in c1["text"]

    filtered = ch.extract_tool_summary_chunks(conn, conversation_ids={"c1"})
    assert len(filtered) == 1 and filtered[0]["conversation_id"] == "c1"

    conn.close()
