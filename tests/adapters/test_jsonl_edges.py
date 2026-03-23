from siftd.adapters._jsonl import parse_block


def test_parse_block_accepts_plain_string_block():
    out = parse_block("hello")
    assert out.block_type == "text"
    assert out.content == {"text": "hello"}
