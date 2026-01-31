"""Smoke tests for token-aware chunking."""

import pytest

pytest.importorskip("fastembed")

from fastembed import TextEmbedding

from siftd.embeddings.chunker import chunk_text


@pytest.fixture(scope="module")
def tokenizer():
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
