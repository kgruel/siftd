"""Smoke tests for token-aware chunking."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fastembed import TextEmbedding
from embeddings.chunker import chunk_text


def get_tokenizer():
    emb = TextEmbedding("BAAI/bge-small-en-v1.5")
    return emb.model.tokenizer


def test_short_text_passthrough():
    """Text already under target_tokens passes through unchanged."""
    tok = get_tokenizer()
    text = "Hello, this is a short sentence."
    result = chunk_text(text, tok, target_tokens=256, max_tokens=512)
    assert result == [text]


def test_empty_text():
    """Empty/whitespace text returns empty list."""
    tok = get_tokenizer()
    assert chunk_text("", tok) == []
    assert chunk_text("   ", tok) == []


def test_long_text_chunks_within_max():
    """All chunks from a >1000 token text are within max_tokens."""
    tok = get_tokenizer()
    tok.no_truncation()

    # Build a text that's well over 1000 tokens
    # Each sentence is ~10 tokens, so 200 sentences ≈ 2000 tokens
    sentences = [f"Sentence number {i} contains several words for testing." for i in range(200)]
    text = " ".join(sentences)

    # Verify input is actually long
    input_tokens = len(tok.encode(text).ids) - 2  # minus special tokens
    assert input_tokens > 1000, f"Expected >1000 tokens, got {input_tokens}"

    max_tokens = 512
    chunks = chunk_text(text, tok, target_tokens=256, max_tokens=max_tokens, overlap_tokens=25)

    assert len(chunks) > 1, f"Expected multiple chunks, got {len(chunks)}"

    for i, chunk in enumerate(chunks):
        # Token count INCLUDING special tokens must fit model max_seq_length
        token_count = len(tok.encode(chunk).ids)
        assert token_count <= max_tokens, (
            f"Chunk {i} has {token_count} tokens (max {max_tokens}): {chunk[:80]}..."
        )


def test_overlap_exists():
    """Adjacent chunks share some content when overlap_tokens > 0."""
    tok = get_tokenizer()

    sentences = [f"Unique sentence {i} with distinct content here." for i in range(100)]
    text = " ".join(sentences)

    chunks = chunk_text(text, tok, target_tokens=100, max_tokens=200, overlap_tokens=25)

    # At least one pair of adjacent chunks should share a word sequence
    found_overlap = False
    for i in range(len(chunks) - 1):
        # Check if the end of chunk[i] overlaps with start of chunk[i+1]
        words_end = chunks[i].split()[-5:]
        words_start = chunks[i + 1].split()[:20]
        for word in words_end:
            if word in " ".join(words_start):
                found_overlap = True
                break
        if found_overlap:
            break

    assert found_overlap, "Expected overlap between adjacent chunks"


if __name__ == "__main__":
    print("Running chunker smoke tests...")
    test_empty_text()
    print("  PASS: empty text")
    test_short_text_passthrough()
    print("  PASS: short text passthrough")
    test_long_text_chunks_within_max()
    print("  PASS: long text chunks within max_tokens")
    test_overlap_exists()
    print("  PASS: overlap exists")
    print("All tests passed.")
