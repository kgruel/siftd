from pathlib import Path

from siftd.adapters import sdk
from siftd.adapters.sdk import NormalizedRecord


class TestDiscoverFilesContainment:
    """discover_files must not yield files whose resolved path escapes the scan base."""

    def test_symlink_escaping_base_is_skipped(self, tmp_path):
        base = tmp_path / "adapter"
        base.mkdir()
        outside = tmp_path / "outside" / "secret.jsonl"
        outside.parent.mkdir()
        outside.write_text("{}")
        link = base / "escape.jsonl"
        link.symlink_to(outside)

        results = list(sdk.discover_files([base], [], ["*.jsonl"]))
        assert not any("escape" in str(s.location) for s in results)

    def test_symlink_inside_base_is_yielded(self, tmp_path):
        base = tmp_path / "adapter"
        base.mkdir()
        real = base / "real.jsonl"
        real.write_text("{}")
        link = base / "linked.jsonl"
        link.symlink_to(real)

        results = list(sdk.discover_files([base], [], ["*.jsonl"]))
        locations = [s.location for s in results]
        assert any("linked" in str(loc) for loc in locations)

    def test_regular_file_is_yielded(self, tmp_path):
        base = tmp_path / "adapter"
        base.mkdir()
        f = base / "session.jsonl"
        f.write_text("{}")

        results = list(sdk.discover_files([base], [], ["*.jsonl"]))
        assert len(results) == 1

    def test_default_locations_also_filters_escapees(self, tmp_path):
        base = tmp_path / "adapter"
        base.mkdir()
        outside = tmp_path / "outside" / "x.jsonl"
        outside.parent.mkdir()
        outside.write_text("{}")
        (base / "escape.jsonl").symlink_to(outside)

        results = list(sdk.discover_files(None, [str(base)], ["*.jsonl"]))
        assert not any("escape" in str(s.location) for s in results)


def test_seek_last_lines_returns_empty_when_binary_open_fails(monkeypatch, tmp_path):
    p = tmp_path / "big.log"
    p.write_text("x\n" * 20000)

    orig_open = Path.open

    def fake_open(self, mode="r", *args, **kwargs):
        if self == p and mode == "rb":
            raise OSError("boom")
        return orig_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)

    assert sdk.seek_last_lines(p, 5) == []


def _usage_normalizer(raw: dict) -> NormalizedRecord:
    return NormalizedRecord(
        kind=raw["kind"],
        timestamp=raw.get("ts"),
        content_blocks=[{"type": "text", "text": raw.get("text", "")}] if raw["kind"] != "usage" else [],
        input_tokens=raw.get("in", 0),
        output_tokens=raw.get("out", 0),
        message_id=raw.get("mid"),
    )


def test_peek_message_id_charges_last_record_and_follows_it_across_exchanges():
    """A message_id's usage is charged once, at its last record's value and exchange."""
    records = [
        {"kind": "user", "text": "first"},
        {"kind": "assistant", "mid": "m1", "in": 1, "out": 2},
        {"kind": "assistant", "mid": "m2", "in": 4, "out": 40},
        {"kind": "user", "text": "second"},
        {"kind": "assistant", "mid": "m1", "in": 1, "out": 30},  # m1's final record
        {"kind": "usage", "in": 7, "out": 3},  # unkeyed usage keeps adding
    ]
    first, second = sdk.peek_exchanges_from_records(records, _usage_normalizer, last_n=5)
    assert (first.input_tokens, first.output_tokens) == (4, 40)
    assert (second.input_tokens, second.output_tokens) == (8, 33)


def test_peek_unkeyed_records_keep_summing():
    records = [
        {"kind": "user", "text": "p"},
        {"kind": "assistant", "in": 2, "out": 5},
        {"kind": "assistant", "in": 3, "out": 6},
    ]
    (exchange,) = sdk.peek_exchanges_from_records(records, _usage_normalizer, last_n=5)
    assert (exchange.input_tokens, exchange.output_tokens) == (5, 11)
