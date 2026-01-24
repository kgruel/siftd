"""Token-aware text chunking using semantic-text-splitter.

Wraps semantic-text-splitter's Rust-based splitter with the fastembed
tokenizer to produce chunks that respect model max_seq_length while
splitting at paragraph/sentence boundaries.
"""

from __future__ import annotations

from tokenizers import Tokenizer
from semantic_text_splitter import TextSplitter


# The tokenizer adds [CLS] and [SEP] (2 special tokens) to every encode.
# semantic-text-splitter counts content tokens only, so we subtract these
# from our limits to ensure encoded chunks fit within model max_seq_length.
_SPECIAL_TOKENS = 2


def chunk_text(
    text: str,
    tokenizer: Tokenizer,
    target_tokens: int = 256,
    max_tokens: int = 512,
    overlap_tokens: int = 25,
) -> list[str]:
    """Split text into token-bounded chunks at semantic boundaries.

    Short texts (already within target_tokens) pass through unchanged.

    Args:
        text: The text to chunk.
        tokenizer: A tokenizers.Tokenizer instance (e.g. from fastembed).
        target_tokens: Preferred chunk size in tokens. Chunks fill to at least this.
        max_tokens: Hard ceiling for chunk size in tokens.
        overlap_tokens: Number of tokens of overlap between adjacent chunks.

    Returns:
        List of text chunks, each within max_tokens when tokenized.
    """
    if not text or not text.strip():
        return []

    # Disable truncation so we can measure true token count
    tokenizer.no_truncation()

    # Check if text is already short enough
    token_count = len(tokenizer.encode(text).ids) - _SPECIAL_TOKENS
    if token_count <= target_tokens:
        return [text]

    # Adjust limits to account for special tokens the model will add
    effective_target = target_tokens - _SPECIAL_TOKENS
    effective_max = max_tokens - _SPECIAL_TOKENS

    splitter = TextSplitter.from_huggingface_tokenizer(
        tokenizer,
        capacity=(effective_target, effective_max),
        overlap=overlap_tokens,
    )

    return splitter.chunks(text)
