"""Token-aware text chunking for embeddings.

Provides:
- chunk_text(): splits a single text into token-bounded chunks with overlap
- extract_exchange_window_chunks(): groups prompt+response exchanges into
  token-bounded windows, the primary chunking strategy for siftd ask --index
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from siftd.storage.queries import fetch_conversation_exchanges

if TYPE_CHECKING:
    from tokenizers import Tokenizer


# --- Low-level: split a single text into token-bounded chunks ---


def chunk_text(
    text: str,
    tokenizer: Tokenizer,
    target_tokens: int = 256,
    max_tokens: int = 512,
    overlap_tokens: int = 25,
) -> list[str]:
    """Split text into token-bounded chunks with overlap.

    - If text fits within target_tokens, returns it as-is.
    - Otherwise splits at sentence/word boundaries aiming for target_tokens,
      never exceeding max_tokens, with overlap_tokens of context between chunks.
    """
    text = text.strip()
    if not text:
        return []

    token_count = _count_tokens(tokenizer, text)
    if token_count <= target_tokens:
        return [text]

    return _split_with_overlap(tokenizer, text, target_tokens, max_tokens, overlap_tokens)


def _count_tokens(tokenizer: Tokenizer, text: str) -> int:
    """Count tokens excluding special tokens."""
    ids = tokenizer.encode(text).ids
    # Subtract 2 for [CLS] and [SEP] special tokens
    return max(0, len(ids) - 2)


def _split_with_overlap(
    tokenizer: Tokenizer,
    text: str,
    target_tokens: int,
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    """Split text into overlapping chunks at sentence/word boundaries."""
    # Split into sentences first, fall back to words
    sentences = _split_sentences(text)

    chunks: list[str] = []
    current_parts: list[str] = []
    current_tokens = 0

    for sentence in sentences:
        sent_tokens = _count_tokens(tokenizer, sentence)

        # If a single sentence exceeds max, split it by words
        if sent_tokens > max_tokens:
            # Flush current buffer
            if current_parts:
                chunks.append(" ".join(current_parts))
                current_parts = []
                current_tokens = 0
            # Split long sentence by words
            word_chunks = _split_words(tokenizer, sentence, target_tokens, max_tokens, overlap_tokens)
            chunks.extend(word_chunks)
            continue

        # Would adding this sentence exceed target?
        if current_tokens + sent_tokens > target_tokens and current_parts:
            chunk_text_str = " ".join(current_parts)
            chunks.append(chunk_text_str)

            # Overlap: keep trailing sentences that fit within overlap_tokens
            overlap_parts: list[str] = []
            overlap_count = 0
            for part in reversed(current_parts):
                part_tokens = _count_tokens(tokenizer, part)
                if overlap_count + part_tokens > overlap_tokens:
                    break
                overlap_parts.insert(0, part)
                overlap_count += part_tokens

            current_parts = overlap_parts + [sentence]
            current_tokens = overlap_count + sent_tokens
        else:
            current_parts.append(sentence)
            current_tokens += sent_tokens

    if current_parts:
        chunks.append(" ".join(current_parts))

    return chunks


def _split_words(
    tokenizer: Tokenizer,
    text: str,
    target_tokens: int,
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    """Split text by words into token-bounded chunks."""
    words = text.split()
    chunks: list[str] = []
    current_words: list[str] = []
    current_tokens = 0

    for word in words:
        word_tokens = _count_tokens(tokenizer, word)
        if current_tokens + word_tokens > target_tokens and current_words:
            chunks.append(" ".join(current_words))

            # Overlap
            overlap_words: list[str] = []
            overlap_count = 0
            for w in reversed(current_words):
                wt = _count_tokens(tokenizer, w)
                if overlap_count + wt > overlap_tokens:
                    break
                overlap_words.insert(0, w)
                overlap_count += wt

            current_words = overlap_words + [word]
            current_tokens = overlap_count + word_tokens
        else:
            current_words.append(word)
            current_tokens += word_tokens

    if current_words:
        chunks.append(" ".join(current_words))

    return chunks


def _split_sentences(text: str) -> list[str]:
    """Naive sentence splitting on period/newline boundaries."""
    import re
    # Split on sentence-ending punctuation followed by space, or newlines
    parts = re.split(r'(?<=[.!?])\s+|\n+', text)
    return [p.strip() for p in parts if p.strip()]


# --- High-level: exchange-window chunking from main DB ---


def extract_exchange_window_chunks(
    main_conn: sqlite3.Connection,
    tokenizer: Tokenizer,
    *,
    target_tokens: int = 256,
    max_tokens: int = 512,
    overlap_tokens: int = 25,
    exclude_conversation_ids: set[str] | None = None,
    conversation_id: str | None = None,
) -> list[dict]:
    """Extract exchange-window chunks from the main DB.

    Groups prompt+response into exchanges per conversation, accumulates
    into token-bounded windows, and splits oversized exchanges.

    Returns list of dicts with keys: conversation_id, chunk_type, text,
    token_count, source_ids.
    """
    exchanges = _load_exchanges(main_conn, exclude_conversation_ids, conversation_id)

    chunks: list[dict] = []
    for conv_id, conv_exchanges in exchanges.items():
        conv_chunks = _window_exchanges(
            conv_exchanges, tokenizer, target_tokens, max_tokens, overlap_tokens
        )
        for text, token_count, prompt_ids in conv_chunks:
            chunks.append({
                "conversation_id": conv_id,
                "chunk_type": "exchange",
                "text": text,
                "token_count": token_count,
                "source_ids": prompt_ids,
            })

    return chunks


def _load_exchanges(
    conn: sqlite3.Connection,
    exclude_ids: set[str] | None,
    conversation_id: str | None,
) -> dict[str, list[dict]]:
    """Load prompt/response pairs grouped by conversation, ordered by timestamp.

    Each exchange is: {"text": str, "prompt_id": str}
    """
    exchanges = fetch_conversation_exchanges(
        conn, conversation_id=conversation_id
    )

    if exclude_ids:
        exchanges = {
            cid: exs for cid, exs in exchanges.items() if cid not in exclude_ids
        }

    return exchanges


def _window_exchanges(
    exchanges: list[dict],
    tokenizer: Tokenizer,
    target_tokens: int,
    max_tokens: int,
    overlap_tokens: int,
) -> list[tuple[str, int, list[str]]]:
    """Accumulate exchanges into token-bounded windows.

    Returns list of (text, token_count, prompt_ids) tuples.
    """
    windows: list[tuple[str, int, list[str]]] = []
    current_parts: list[str] = []
    current_ids: list[str] = []
    current_tokens = 0

    for exchange in exchanges:
        text = exchange["text"]
        prompt_id = exchange["prompt_id"]
        token_count = _count_tokens(tokenizer, text)

        # If single exchange exceeds max, split it
        if token_count > max_tokens:
            # Flush current window
            if current_parts:
                window_text = "\n\n".join(current_parts)
                windows.append((window_text, _count_tokens(tokenizer, window_text), current_ids))
                current_parts = []
                current_ids = []
                current_tokens = 0

            # Split the oversized exchange — all sub-chunks reference the same prompt
            sub_chunks = chunk_text(text, tokenizer, target_tokens, max_tokens, overlap_tokens)
            for sc in sub_chunks:
                windows.append((sc, _count_tokens(tokenizer, sc), [prompt_id]))
            continue

        # Would adding this exchange exceed target?
        if current_tokens + token_count > target_tokens and current_parts:
            window_text = "\n\n".join(current_parts)
            windows.append((window_text, _count_tokens(tokenizer, window_text), current_ids))
            current_parts = []
            current_ids = []
            current_tokens = 0

        current_parts.append(text)
        current_ids.append(prompt_id)
        current_tokens += token_count

    # Flush remaining
    if current_parts:
        window_text = "\n\n".join(current_parts)
        windows.append((window_text, _count_tokens(tokenizer, window_text), current_ids))

    return windows
