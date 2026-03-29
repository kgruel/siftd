from siftd.adapters._jsonl import load_jsonl, parse_block


def test_parse_block_accepts_plain_string_block():
    out = parse_block("hello")
    assert out.block_type == "text"
    assert out.content == {"text": "hello"}


def test_load_jsonl_skips_malformed_lines(tmp_path):
    """Truncated/invalid JSON lines are skipped, not fatal."""
    f = tmp_path / "bad.jsonl"
    f.write_text('{"type": "good"}\n{"truncated\n{"type": "also_good"}\n')
    records = load_jsonl(f)
    assert len(records) == 2
    assert records[0]["type"] == "good"
    assert records[1]["type"] == "also_good"


def test_load_jsonl_filters_non_dict_records(tmp_path):
    """Non-dict JSON values (arrays, scalars) are filtered out."""
    f = tmp_path / "mixed.jsonl"
    f.write_text('{"type": "ok"}\n[1, 2, 3]\n"bare string"\n42\n{"type": "ok2"}\n')
    records = load_jsonl(f)
    assert len(records) == 2
    assert records[0]["type"] == "ok"
    assert records[1]["type"] == "ok2"
