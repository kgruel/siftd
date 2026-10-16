"""Pi Agent decoding: each test names the placement or conversion it pins, and the defect it replaces.

The characterization file this replaces froze the old parser's defects (a
character per block, an image kept raw, an orphan response dropped). These
tests assert the shared pipeline's behavior instead.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from siftd.adapters import pi_agent
from siftd.adapters.assembly import assemble, read_jsonl
from siftd.adapters.records import Disposition, Ignored, Unknown
from siftd.domain import Conversation
from siftd.domain.source import Source
from siftd.storage.sql_helpers import uncached_input_sql

TS = "2026-01-01T00:00:00Z"
GOLDEN = Path(__file__).resolve().parent.parent / "fixtures" / "adapters" / "pi_agent"


def _header(sid="s1", cwd="/w"):
    return {"type": "session", "version": 3, "id": sid, "timestamp": TS, "cwd": cwd}


def _user(at, content, **extra):
    return {"type": "message", "timestamp": at, "message": {"role": "user", "content": content, **extra}}


def _assistant(at, content, **message):
    return {"type": "message", "timestamp": at, "message": {"role": "assistant", "model": "m", "provider": "anthropic", "content": content, **message}}


def _result(at, call_id, content, **extra):
    return {"type": "message", "timestamp": at, "message": {"role": "toolResult", "toolCallId": call_id, "toolName": "read", "content": content, "isError": False, **extra}}


def _call(call_id, name="read", arguments=None):
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments if arguments is not None else {"path": "/w/a"}}


def _assembly(tmp_path, records, name="s.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return assemble(
        Source(kind="file", location=path),
        read=read_jsonl,
        decoder=pi_agent.PiDecoder,
        harness=pi_agent.HARNESS,
        default_session=pi_agent._default_session(path),
    )


def _only(tmp_path, records) -> Conversation:
    (conversation,) = _assembly(tmp_path, records).conversations
    return conversation


def _responses(conversation):
    return list(conversation.preamble) + [r for p in conversation.prompts for r in p.responses]


def _calls(conversation):
    return [c for r in _responses(conversation) for c in r.tool_calls]


class TestDecoderTable:
    def test_every_record_type_is_named_and_the_table_matches_the_code(self):
        """The decoder's docstring table is the record ledger; it must not drift from what the code does."""
        rows = []
        for line in pi_agent.PiDecoder.__doc__.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.strip().startswith("| `") and len(cells) == 3:
                rows.append((cells[0], cells[1]))
        assert len(rows) >= 13

        decoder = pi_agent.PiDecoder(Source(kind="file", location=Path("/x/s.jsonl")))
        ignored_types: set[str] = set()
        ignored_roles: set[str] = set()
        for name, becomes in rows:
            kind = re.match(r"`([^`]+)`", name).group(1)
            if "any other kind" in name:
                continue  # named as Unknown by design: an unseen usage kind is not guessed at
            unit = {"type": kind, "timestamp": TS, "id": "x"}
            role = re.search(r"role `([^`]+)`", name)
            if role:
                unit["message"] = {"role": role.group(1), "content": "x", "toolCallId": "c", "toolName": "t"}
            if kind_match := re.search(r"kind `([^`]+)`", name):
                unit.update(kind=kind_match.group(1), usage={"input": 1, "output": 1})
            if kind in ("custom_message", "custom"):
                unit["customType"] = "x"
            records = decoder.decode(unit)
            assert records and not any(isinstance(r, Unknown) for r in records), name
            if becomes.startswith("Ignored"):
                assert any(isinstance(r, Ignored) for r in records), name
                if role:
                    ignored_roles.add(role.group(1))
                else:
                    ignored_types.add(kind)

        assert ignored_types == pi_agent._IGNORED_TYPES | {"custom_message", "custom"}
        assert ignored_roles == pi_agent._IGNORED_ROLES

    def test_an_unrecognized_type_is_unknown_and_counted(self, tmp_path):
        ledger = _assembly(tmp_path, [_header(), _user(TS, "hi"), {"type": "a-new-type", "timestamp": TS}]).ledger
        assert ledger.reasons[(Disposition.UNKNOWN, "type:a-new-type")] == 1
        assert ledger.disposed == ledger.records

    def test_a_message_with_no_object_is_unknown_not_a_crash(self, tmp_path):
        ledger = _assembly(tmp_path, [_header(), _user(TS, "hi"), {"type": "message", "timestamp": TS, "message": None}]).ledger
        assert ledger.reasons[(Disposition.UNKNOWN, "message:no-object")] == 1


class TestIdentity:
    def test_the_conversation_is_named_by_the_last_header_id(self, tmp_path):
        """The old parser took the last header's id, so a file with two keeps that name and stays one conversation."""
        conversation = _only(tmp_path, [_header("first"), _user(TS, "hi"), _header("second")])
        assert conversation.external_id == "pi_agent::second"

    def test_a_file_without_a_header_is_named_for_its_stem(self, tmp_path):
        conversation = _only(tmp_path, [_user(TS, "hi"), _assistant(TS, [{"type": "text", "text": "ok"}])])
        assert conversation.external_id == "pi_agent::s"

    def test_the_file_reader_and_the_in_memory_parse_agree(self, tmp_path):
        """The identity preflight parses captured records; it must see the same conversations parse() yields."""
        records = [_header(), _user(TS, "hi"), _assistant(TS, [_call("c1")]), _result(TS, "c1", [{"type": "text", "text": "ok"}])]
        path = tmp_path / "s.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        from_file = list(pi_agent.parse(Source(kind="file", location=path)))
        assert from_file == pi_agent._parse_records(records, path)


class TestContent:
    def test_string_content_is_one_text_block(self, tmp_path):
        """The old parser iterated the string, so \"hello\" became five one-character blocks."""
        conversation = _only(tmp_path, [_header(), _user(TS, "hello")])
        (block,) = conversation.prompts[0].content
        assert (block.block_type, block.content) == ("text", {"text": "hello"})

    def test_a_user_image_goes_through_the_binary_filter(self, tmp_path):
        image = {"type": "image", "data": "A" * 800, "mimeType": "image/png"}
        conversation = _only(tmp_path, [_header(), _user(TS, [{"type": "text", "text": "look"}, image])])
        image_block = conversation.prompts[0].content[1]
        assert image_block.block_type == "image"
        assert image_block.content["source"]["type"] == "filtered"
        assert "A" * 20 not in json.dumps(image_block.content)

    def test_a_tool_result_image_is_kept_as_a_filtered_placeholder(self, tmp_path):
        """The old parser dropped result images; now they are kept as metadata, never as data."""
        image = {"type": "image", "data": "B" * 900, "mimeType": "image/jpeg"}
        conversation = _only(tmp_path, [
            _header(),
            _user(TS, "go"),
            _assistant(TS, [_call("c1")]),
            _result(TS, "c1", [{"type": "text", "text": "done"}, image]),
        ])
        (call,) = _calls(conversation)
        assert call.result["output"] == "done"
        (placed,) = call.result["content"]
        assert placed["source"] == {"type": "filtered", "original_type": "base64", "media_type": "image/jpeg",
                                    "original_size": 900, "filtered_reason": "binary_content"}
        assert "B" * 20 not in json.dumps(call.result)

    def test_a_result_with_no_text_is_an_empty_output_not_none(self, tmp_path):
        """A completed call keeps a result. The 13 corpus results with empty text were None before."""
        conversation = _only(tmp_path, [
            _header(), _user(TS, "go"), _assistant(TS, [_call("c1")]), _result(TS, "c1", [{"type": "text", "text": ""}]),
        ])
        (call,) = _calls(conversation)
        assert call.status == "success" and call.result == {"output": ""}

    def test_the_empty_result_capture_still_holds_an_empty_result(self):
        """The empty_result golden is named for an empty result. Redaction once replaced every result's text,
        so the golden exercised nothing; this pins that the capture still holds one, and that its
        expected output is the empty string, not None."""
        lines = (GOLDEN / "empty_result" / "input.jsonl").read_text(encoding="utf-8").splitlines()
        results = [json.loads(line)["message"] for line in lines if '"toolResult"' in line]
        assert any(m["content"] == [{"type": "text", "text": ""}] for m in results)
        expected = json.loads((GOLDEN / "empty_result" / "expected.json").read_text(encoding="utf-8"))
        outputs = [c.get("result") for conv in expected for p in conv["prompts"] for r in p["responses"] for c in r.get("tool_calls", [])]
        assert {"output": ""} in outputs

    def test_a_thinking_block_keeps_its_text(self, tmp_path):
        conversation = _only(tmp_path, [_header(), _user(TS, "q"), _assistant(TS, [{"type": "thinking", "thinking": "t"}, {"type": "text", "text": "a"}])])
        response = conversation.prompts[0].responses[0]
        assert [(b.block_type, b.content) for b in response.content] == [("thinking", {"text": "t"}), ("text", {"text": "a"})]


class TestUsage:
    def test_cache_reads_are_added_back_to_input(self, tmp_path):
        """pi's input excludes cache reads; siftd stores it inclusive, so the cache reads are added back."""
        conversation = _only(tmp_path, [
            _header(), _user(TS, "q"),
            _assistant(TS, [], usage={"input": 12007, "output": 439, "cacheRead": 4352, "cacheWrite": 0}),
        ])
        response = conversation.prompts[0].responses[0]
        assert response.usage.input_tokens == 16359 and response.usage.output_tokens == 439
        assert response.attributes["cache_read_input_tokens"] == "4352"
        assert response.attributes["cache_creation_input_tokens"] == "0"

    @pytest.mark.parametrize("raw_input,cache_read", [(12007, 4352), (13867, 16256)], ids=["input-above-cache", "input-below-cache"])
    def test_storage_reads_back_pis_uncached_input_for_both_classes(self, tmp_path, raw_input, cache_read):
        """The over-subtraction: rows with input at or above cacheRead were read as inclusive. Both classes must read back raw input."""
        conversation = _only(tmp_path, [
            _header(), _user(TS, "q"),
            _assistant(TS, [], usage={"input": raw_input, "output": 1, "cacheRead": cache_read, "cacheWrite": 0}),
        ])
        usage = conversation.prompts[0].responses[0].usage
        cache = int(conversation.prompts[0].responses[0].attributes["cache_read_input_tokens"])
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE r (input_tokens INTEGER, cache_read INTEGER, source TEXT)")
        conn.execute("INSERT INTO r VALUES (?, ?, 'multi')", (usage.input_tokens, cache))
        (uncached,) = conn.execute(f"SELECT {uncached_input_sql('r')} FROM r").fetchone()
        assert uncached == raw_input

    def test_a_cache_warm_usage_record_sums_onto_the_latest_response(self, tmp_path):
        """A standalone usage record is billed tokens with no message; it sums onto the response before it."""
        conversation = _only(tmp_path, [
            _header(), _user(TS, "q"),
            _assistant(TS, [], usage={"input": 2, "output": 3, "cacheRead": 10, "cacheWrite": 0}),
            {"type": "usage", "timestamp": TS, "kind": "cache_warm", "provider": "anthropic", "model": "m",
             "usage": {"input": 1, "output": 5, "cacheRead": 100, "cacheWrite": 7}},
        ])
        response = conversation.prompts[0].responses[0]
        assert response.usage.input_tokens == (2 + 10) + (1 + 100) and response.usage.output_tokens == 3 + 5
        assert response.attributes["cache_read_input_tokens"] == str(10 + 100)
        assert response.attributes["cache_creation_input_tokens"] == str(0 + 7)

    def test_a_cache_warm_for_another_model_is_ignored_not_billed_to_the_latest_response(self, tmp_path):
        """After a model switch the warm names the new model, and the latest response is the old one.

        Summed onto the old response it inflated that model's cache reads (220,800 -> 545,290 on the
        real file). pi names no response to attach it to, so it is named and kept out of the totals.
        """
        assembly = _assembly(tmp_path, [
            _header(), _user(TS, "q"),
            _assistant(TS, [{"type": "text", "text": "a"}], provider="openai-codex", model="gpt-6-astra",
                       usage={"input": 2, "output": 3, "cacheRead": 10, "cacheWrite": 0}),
            {"type": "usage", "timestamp": TS, "kind": "cache_warm", "provider": "anthropic", "model": "claude-fable-5-1",
             "usage": {"input": 1, "output": 1, "cacheRead": 100, "cacheWrite": 0}},
        ])
        (conversation,) = assembly.conversations
        response = conversation.prompts[0].responses[0]
        assert response.usage.input_tokens == 2 + 10 and response.usage.output_tokens == 3
        assert response.attributes["cache_read_input_tokens"] == "10"
        assert assembly.ledger.reasons[(Disposition.IGNORED, "usage:cache_warm:unattributed")] == 1

    def test_a_cache_warm_that_names_no_model_is_ignored(self, tmp_path):
        """Unnamed tokens cannot be shown to belong to the latest response, so they are not summed onto it."""
        warm = {"type": "usage", "timestamp": TS, "kind": "cache_warm", "usage": {"input": 1, "output": 1, "cacheRead": 100, "cacheWrite": 0}}
        assembly = _assembly(tmp_path, [_header(), _user(TS, "q"), _assistant(TS, [{"type": "text", "text": "a"}]), warm])
        assert assembly.ledger.reasons[(Disposition.IGNORED, "usage:cache_warm:unattributed")] == 1
        assert assembly.ledger.count(Disposition.UNKNOWN) == 0

    def test_a_cache_warm_before_any_response_is_ignored_not_invented_onto(self, tmp_path):
        """With no response yet there is nothing to sum onto; the warm is Ignored, and no response is invented."""
        warm = {"type": "usage", "timestamp": TS, "kind": "cache_warm", "provider": "anthropic", "model": "m",
                "usage": {"input": 1, "output": 1, "cacheRead": 100, "cacheWrite": 0}}
        assembly = _assembly(tmp_path, [_header(), _user(TS, "q"), warm])
        assert assembly.ledger.reasons[(Disposition.IGNORED, "usage:cache_warm:unattributed")] == 1
        assert assembly.ledger.dispositions[("ResponseRec", Disposition.EVENT)] == 0

    def test_an_unseen_usage_kind_is_unknown_and_counted(self, tmp_path):
        ledger = _assembly(tmp_path, [_header(), _user(TS, "q"), {"type": "usage", "timestamp": TS, "kind": "new", "usage": {"input": 1}}]).ledger
        assert ledger.reasons[(Disposition.UNKNOWN, "usage:new")] == 1

    def test_the_cost_attribute_is_kept_as_written(self, tmp_path):
        conversation = _only(tmp_path, [_header(), _user(TS, "q"), _assistant(TS, [], usage={"input": 1, "output": 1, "cost": {"total": 0.25}})])
        assert conversation.prompts[0].responses[0].attributes["cost"] == "0.25"


class TestProvider:
    @pytest.mark.parametrize("raw,stored", [
        ("openai-codex", "openai"),
        ("google-gemini-cli", "google"),
        ("anthropic", "anthropic"),
        ("github-copilot", "github-copilot"),
    ])
    def test_a_response_carries_its_own_provider_under_siftd_s_name(self, tmp_path, raw, stored):
        """Responses were stored under the harness source 'multi', which no price is filed under."""
        conversation = _only(tmp_path, [_header(), _user(TS, "q"), _assistant(TS, [{"type": "text", "text": "a"}], provider=raw)])
        assert conversation.prompts[0].responses[0].provider == stored


class TestPlacement:
    def test_an_ignored_record_still_bounds_the_conversation(self, tmp_path):
        """Conversation bounds span every record's time, as they always have; an Ignored record carries its time as Meta."""
        late = "2026-01-05T00:00:00Z"
        assembly = _assembly(tmp_path, [
            _header(), _user(TS, "q"), _assistant(TS, [{"type": "text", "text": "a"}]),
            {"type": "custom_message", "customType": "workflow-result", "timestamp": late, "content": "x"},
        ])
        (conversation,) = assembly.conversations
        assert conversation.ended_at == late
        assert assembly.ledger.reasons[(Disposition.IGNORED, "custom_message:workflow-result")] == 1
        assert assembly.ledger.dispositions[("Meta", Disposition.META)] >= 1

    def test_a_result_with_no_call_is_an_orphan_not_a_silent_drop(self, tmp_path):
        ledger = _assembly(tmp_path, [_header(), _user(TS, "q"), _assistant(TS, []), _result(TS, "missing", [])]).ledger
        assert ledger.reasons[(Disposition.ORPHAN, "tool result with no call")] == 1

    def test_a_response_before_any_prompt_is_kept_in_the_preamble(self, tmp_path):
        """Nothing is dropped, and no prompt is invented for it."""
        conversation = _only(tmp_path, [_header(), _assistant(TS, [_call("c1")]), _result(TS, "c1", [{"type": "text", "text": "ok"}])])
        assert conversation.prompts == [] and len(conversation.preamble) == 1
        (call,) = conversation.preamble[0].tool_calls
        assert call.status == "success"

    def test_a_result_without_a_call_id_is_unknown(self, tmp_path):
        ledger = _assembly(tmp_path, [_header(), _user(TS, "q"), _assistant(TS, []),
                                      {"type": "message", "timestamp": TS, "message": {"role": "toolResult", "content": []}}]).ledger
        assert ledger.reasons[(Disposition.UNKNOWN, "toolResult:no-toolCallId")] == 1

    def test_a_call_with_no_result_stays_pending(self, tmp_path):
        conversation = _only(tmp_path, [_header(), _user(TS, "q"), _assistant(TS, [_call("c1")])])
        (call,) = _calls(conversation)
        assert call.status == "pending" and call.result is None and call.timestamp == TS

    def test_a_repeated_call_id_keeps_both_calls(self, tmp_path):
        """The old parser overwrote the first call's pairing; both calls are kept, and the result pairs the earliest open one."""
        conversation = _only(tmp_path, [
            _header(), _user(TS, "q"),
            _assistant(TS, [_call("dup", "read"), _call("dup", "bash")]),
            _result(TS, "dup", [{"type": "text", "text": "ok"}]),
        ])
        calls = _calls(conversation)
        assert [c.tool_name for c in calls] == ["read", "bash"]
        assert calls[0].status == "success" and calls[1].status == "pending"

    def test_a_header_only_file_yields_no_conversation(self, tmp_path):
        """The old parser yielded a hollow row with no prompts. The assembler yields none, and counts the empty file."""
        assembly = _assembly(tmp_path, [_header(), {"type": "model_change", "timestamp": TS, "modelId": "m", "provider": "anthropic"}])
        assert assembly.conversations == []
        assert assembly.ledger.empty_conversations == 1

    def test_model_change_gives_later_responses_their_model(self, tmp_path):
        conversation = _only(tmp_path, [
            _header(), {"type": "model_change", "timestamp": TS, "modelId": "ctx", "provider": "anthropic"},
            _user(TS, "q"), {"type": "message", "timestamp": TS, "message": {"role": "assistant", "content": [{"type": "text", "text": "a"}]}},
        ])
        assert conversation.prompts[0].responses[0].model == "ctx"

    def test_a_malformed_line_is_counted(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text(json.dumps(_header()) + "\n{truncated\n" + json.dumps(_user(TS, "q")) + "\n")
        assembly = assemble(Source(kind="file", location=path), read=read_jsonl, decoder=pi_agent.PiDecoder,
                            harness=pi_agent.HARNESS, default_session=pi_agent._default_session(path))
        assert assembly.ledger.reasons[(Disposition.UNKNOWN, "malformed")] == 1
        assert assembly.ledger.disposed == assembly.ledger.records


class TestPeek:
    def test_peek_counts_the_same_tool_calls_as_parse(self):
        """Peek showed 0 of 24,156 pi tool calls: the record fold read the raw block type."""
        path = GOLDEN / "cache_convention" / "input.jsonl"
        (conversation,) = pi_agent.parse(Source(kind="file", location=path))
        parsed_calls = len(_calls(conversation))
        exchanges = pi_agent.peek_exchanges(path, 10 ** 6)
        assert parsed_calls > 0
        assert sum(len(e.tool_use_ids) for e in exchanges) == parsed_calls

    def test_peek_and_ingest_name_the_same_last_model_and_activity(self):
        path = GOLDEN / "trailing_records" / "input.jsonl"
        (conversation,) = pi_agent.parse(Source(kind="file", location=path))
        scan = pi_agent.peek_scan(path)
        assert scan.model == _responses(conversation)[-1].model
        assert scan.last_activity_at == conversation.ended_at
        assert scan.exchange_count == len(conversation.prompts)


@pytest.mark.parametrize("case", sorted(p.name for p in GOLDEN.iterdir() if (p / "input.jsonl").exists()))
def test_fixtures_decode_with_nothing_unknown_or_orphaned(case):
    path = GOLDEN / case / "input.jsonl"
    ledger = assemble(Source(kind="file", location=path), read=read_jsonl, decoder=pi_agent.PiDecoder,
                      harness=pi_agent.HARNESS, default_session=pi_agent._default_session(path)).ledger
    assert ledger.count(Disposition.UNKNOWN) == 0 and ledger.count(Disposition.ORPHAN) == 0


def test_the_in_memory_variant_yields_nothing_for_nothing(tmp_path):
    assert pi_agent._parse_records([], tmp_path / "s.jsonl") == []
