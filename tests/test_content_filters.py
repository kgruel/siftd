"""Tests for binary content filtering."""

import json

import pytest

from siftd.content.filters import (
    filter_binary_block,
    filter_tool_result_binary,
    has_large_base64,
    is_base64_image_block,
    is_binary_content,
)


class TestIsBase64ImageBlock:
    """Tests for is_base64_image_block detection."""

    def test_image_block_with_base64(self):
        """Image block with base64 source is detected."""
        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            },
        }
        assert is_base64_image_block(block) is True

    def test_document_block_with_base64(self):
        """Document block with base64 source is detected."""
        block = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": "JVBERi0xLjQK...",
            },
        }
        assert is_base64_image_block(block) is True

    def test_text_block_not_detected(self):
        """Text blocks are not detected as binary."""
        block = {"type": "text", "text": "Hello, world!"}
        assert is_base64_image_block(block) is False

    def test_image_block_with_url(self):
        """Image block with URL source (not base64) is not detected."""
        block = {
            "type": "image",
            "source": {"type": "url", "url": "https://example.com/image.png"},
        }
        assert is_base64_image_block(block) is False

    def test_non_dict_not_detected(self):
        """Non-dict values are not detected."""
        assert is_base64_image_block("string") is False
        assert is_base64_image_block(None) is False
        assert is_base64_image_block([]) is False


class TestIsBinaryContent:
    """Tests for is_binary_content detection."""

    def test_null_bytes_detected(self):
        """Content with null bytes is detected as binary."""
        assert is_binary_content("Hello\x00World") is True

    def test_sqlite_header_detected(self):
        """SQLite file magic bytes detected."""
        assert is_binary_content("SQLite format 3\x00\x10...") is True

    def test_png_header_detected(self):
        """PNG file magic bytes detected."""
        assert is_binary_content("\x89PNG\r\n\x1a\n...") is True

    def test_pdf_header_detected(self):
        """PDF file magic bytes detected."""
        assert is_binary_content("%PDF-1.4...") is True

    def test_jpeg_header_detected(self):
        """JPEG file magic bytes detected."""
        assert is_binary_content("\xff\xd8\xff\xe0...") is True

    def test_gif_header_detected(self):
        """GIF file magic bytes detected."""
        assert is_binary_content("GIF89a...") is True
        assert is_binary_content("GIF87a...") is True

    def test_plain_text_not_detected(self):
        """Plain text is not detected as binary."""
        assert is_binary_content("Hello, world!") is False
        assert is_binary_content("Some code: const x = 1;") is False

    def test_json_not_detected(self):
        """JSON content is not detected as binary."""
        assert is_binary_content('{"key": "value"}') is False

    def test_non_string_not_detected(self):
        """Non-string values are not detected as binary."""
        assert is_binary_content(None) is False
        assert is_binary_content(123) is False


class TestHasLargeBase64:
    """Tests for has_large_base64 detection."""

    def test_large_base64_detected(self):
        """Large base64 strings (500+ chars) are detected."""
        base64_data = "A" * 600
        content = f"Some text with base64: {base64_data} more text"
        assert has_large_base64(content) is True

    def test_small_base64_not_detected(self):
        """Small base64 strings are not detected (to avoid JWT false positives)."""
        base64_data = "A" * 100  # Too short
        content = f"Some text with base64: {base64_data}"
        assert has_large_base64(content) is False

    def test_no_base64_not_detected(self):
        """Content without base64 is not detected."""
        assert has_large_base64("Hello, world!") is False
        assert has_large_base64("Some regular text") is False

    def test_non_string_not_detected(self):
        """Non-string values are not detected."""
        assert has_large_base64(None) is False
        assert has_large_base64(123) is False


class TestFilterBinaryBlock:
    """Tests for filter_binary_block transformation."""

    def test_filters_base64_image(self):
        """Image block with base64 is filtered to placeholder."""
        original_data = "iVBORw0KGgoAAAANSUhEUg" + "A" * 1000
        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": original_data,
            },
        }

        filtered = filter_binary_block(block)

        assert filtered["type"] == "image"
        assert filtered["source"]["type"] == "filtered"
        assert filtered["source"]["original_type"] == "base64"
        assert filtered["source"]["media_type"] == "image/png"
        assert filtered["source"]["original_size"] == len(original_data)
        assert filtered["source"]["filtered_reason"] == "binary_content"
        assert "data" not in filtered["source"]

    def test_filters_pdf_document(self):
        """Document block with PDF is filtered."""
        block = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": "JVBERi0xLjQK" + "A" * 1000,
            },
        }

        filtered = filter_binary_block(block)

        assert filtered["type"] == "document"
        assert filtered["source"]["type"] == "filtered"
        assert filtered["source"]["media_type"] == "application/pdf"

    def test_preserves_text_block(self):
        """Text blocks are returned unchanged."""
        block = {"type": "text", "text": "Hello, world!"}
        filtered = filter_binary_block(block)
        assert filtered is block

    def test_preserves_non_binary_block(self):
        """Non-binary blocks are returned unchanged."""
        block = {"type": "tool_use", "id": "123", "name": "Read", "input": {}}
        filtered = filter_binary_block(block)
        assert filtered is block

    def test_preserves_extra_fields(self):
        """Extra fields like cache_control are preserved when filtering."""
        block = {
            "type": "image",
            "cache_control": {"type": "ephemeral"},
            "id": "block_123",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "A" * 1000,
            },
        }

        filtered = filter_binary_block(block)

        assert filtered["type"] == "image"
        assert filtered["cache_control"] == {"type": "ephemeral"}
        assert filtered["id"] == "block_123"
        assert filtered["source"]["type"] == "filtered"
        assert "data" not in filtered["source"]


class TestFilterToolResultBinary:
    """Tests for filter_tool_result_binary transformation."""

    def test_filters_content_list_with_images(self):
        """Content list containing images is filtered."""
        result = {
            "content": [
                {"type": "text", "text": "Here is an image:"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "A" * 1000,
                    },
                },
            ]
        }

        filtered = filter_tool_result_binary(result)

        assert len(filtered["content"]) == 2
        assert filtered["content"][0] == result["content"][0]  # Text unchanged
        assert filtered["content"][1]["source"]["type"] == "filtered"

    def test_filters_binary_string_content(self):
        """Binary string content is filtered."""
        result = {"content": "SQLite format 3\x00\x10..."}

        filtered = filter_tool_result_binary(result)

        assert filtered["content"] == "[binary content filtered]"
        assert filtered["filtered_reason"] == "binary_content"
        assert "original_size" in filtered

    def test_filters_large_base64_string(self):
        """String content with large base64 is filtered."""
        base64_data = "A" * 600
        result = {"content": f"Some text {base64_data} more text"}

        filtered = filter_tool_result_binary(result)

        assert filtered["content"] == "[base64 content filtered]"
        assert filtered["filtered_reason"] == "base64_content"
        assert "original_size" in filtered

    def test_preserves_normal_string_content(self):
        """Normal string content is preserved."""
        result = {"content": "Hello, world!"}

        filtered = filter_tool_result_binary(result)

        assert filtered is result

    def test_preserves_non_content_result(self):
        """Results without 'content' are preserved."""
        result = {"status": "ok", "data": {"key": "value"}}

        filtered = filter_tool_result_binary(result)

        assert filtered is result

    def test_preserves_non_dict_result(self):
        """Non-dict results are preserved."""
        assert filter_tool_result_binary("string") == "string"
        assert filter_tool_result_binary(None) is None
        assert filter_tool_result_binary(123) == 123


class TestFilterIntegration:
    """Integration tests for the full filtering pipeline."""

    def test_round_trip_with_json(self):
        """Filtered content can be serialized to JSON."""
        original = {
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "A" * 1000,
                    },
                }
            ]
        }

        filtered = filter_tool_result_binary(original)
        json_str = json.dumps(filtered)
        parsed = json.loads(json_str)

        assert parsed["content"][0]["source"]["type"] == "filtered"
        assert "data" not in parsed["content"][0]["source"]

    def test_placeholder_contains_size_info(self):
        """Filtered placeholders preserve original size for metrics."""
        data = "A" * 12345
        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": data,
            },
        }

        filtered = filter_binary_block(block)

        assert filtered["source"]["original_size"] == 12345


class TestBackfillFilterBinary:
    """Tests for backfill_filter_binary function."""

    def test_ref_count_adjusted_correctly(self, tmp_path):
        """Verify ref_count is adjusted based on actual reference count."""
        from siftd.backfill import backfill_filter_binary
        from siftd.storage.blobs import compute_content_hash, get_ref_count
        from siftd.storage.sqlite import (
            create_database,
            get_or_create_harness,
            get_or_create_model,
            get_or_create_tool,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        model_id = get_or_create_model(conn, "test-model")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T00:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T00:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, model_id, None, "r1", "2024-01-01T00:00:01Z", 100, 50)
        tool_id = get_or_create_tool(conn, "test_tool")

        # Create a binary result that will be filtered
        binary_result = json.dumps({
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "iVBORw0KGgo" + "A" * 500,
                    },
                }
            ]
        })

        # Insert 3 tool calls all referencing the same blob
        # Signature: (conn, response_id, conversation_id, tool_id, external_id, input_json, result_json, status, timestamp)
        for i in range(3):
            insert_tool_call(
                conn, response_id, conv_id, tool_id, f"tc{i}",
                '{"arg": "value"}', binary_result, "success", "2024-01-01T00:00:02Z",
                filter_binary=False,  # Don't filter on insert - we want the binary data
            )

        conn.commit()

        # Verify initial ref_count is 3
        old_hash = compute_content_hash(binary_result)
        assert get_ref_count(conn, old_hash) == 3

        # Run backfill
        stats = backfill_filter_binary(conn)

        assert stats["filtered"] == 1
        assert stats["skipped"] == 0
        assert stats["errors"] == 0

        # Old blob should be deleted (ref_count went from 3 to 0)
        assert get_ref_count(conn, old_hash) == 0

        # All tool_calls should now point to the new hash
        cur = conn.execute("SELECT DISTINCT result_hash FROM tool_calls")
        hashes = [row[0] for row in cur.fetchall()]
        assert len(hashes) == 1
        new_hash = hashes[0]
        assert new_hash != old_hash

        # New blob should have ref_count of 3
        assert get_ref_count(conn, new_hash) == 3

        conn.close()
