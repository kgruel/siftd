"""Tests for Aider adapter."""

import hashlib
import re
from pathlib import Path

from conftest import FIXTURES_DIR, pinned_tz

from siftd.adapters import aider
from siftd.adapters.assembly import assemble
from siftd.adapters.records import Disposition, Unknown
from siftd.domain import Usage
from siftd.domain.source import Source


class TestAiderAdapter:

    def test_can_handle(self):
        assert aider.can_handle(Source(kind="file", location=Path("/project/.aider.chat.history.md")))
        assert not aider.can_handle(Source(kind="file", location=Path("/project/README.md")))
        assert not aider.can_handle(Source(kind="directory", location=Path("/project")))

    def test_parse_full(self):
        source = Source(kind="file", location=FIXTURES_DIR / ".aider.chat.history.md")
        # Pinned because started_at resolves against the host zone; UTC is the
        # zone in which the rendered value matches the header verbatim.
        with pinned_tz("UTC"):
            convos = list(aider.parse(source))
            assert [c.external_id for c in aider.parse(source)] == [
                c.external_id for c in convos
            ]
        assert len(convos) == 2
        conv = convos[0]
        assert conv.external_id.startswith("aider::") and "2025-07-15 14:32:01" in conv.external_id
        assert conv.started_at == "2025-07-15T14:32:01+00:00" and conv.harness.name == "aider"
        assert conv.workspace_path == str(FIXTURES_DIR) and len(conv.prompts) == 2
        assert "write a hello world script" in conv.prompts[0].content[0].content["text"]
        assert "now add a greeting function" in conv.prompts[1].content[0].content["text"]
        assert "that takes a name parameter" in conv.prompts[1].content[0].content["text"]
        assert [b for b in conv.prompts[0].responses[0].content if b.block_type == "text" and "hello world" in b.content["text"].lower()]
        all_blocks = [b for r in conv.prompts[0].responses for b in r.content]
        assert [b for b in all_blocks if b.block_type == "tool_output" and "Applied edit" in b.content["text"]]
        resp_cost = next((r for r in conv.prompts[0].responses if r.attributes.get("approx_cost")), None)
        assert resp_cost and resp_cost.attributes["approx_cost"] == "0.01"
        assert convos[1].started_at == "2025-07-15T15:10:00+00:00"
        assert "fix the bug in auth.py" in convos[1].prompts[0].content[0].content["text"]

    def test_local_header_time_is_stored_as_utc(self):
        """#31: the header is local wall time; `started_at` is a UTC column.

        `TestLocalToUtc` owns the conversion itself, including what it does to
        a `--since` bound. This asserts the adapter actually applies it — the
        one thing that stays green if aider stops calling it.
        """
        source = Source(kind="file", location=FIXTURES_DIR / ".aider.chat.history.md")
        with pinned_tz("America/Chicago"):  # UTC-5 in July
            conv = next(iter(aider.parse(source)))

        assert conv.started_at == "2025-07-15T19:32:01+00:00"
        # Every timestamp the conversation carries moves with it.
        assert conv.prompts[0].timestamp == "2025-07-15T19:32:01+00:00"
        assert conv.prompts[0].responses[0].timestamp == "2025-07-15T19:32:01+00:00"

    def test_external_id_keeps_the_raw_header_time(self):
        """The dedup key must not move with the zone.

        `external_id` is UNIQUE per harness; re-keying it on the UTC value
        would duplicate every already-ingested aider conversation, and would
        make the same file ingest differently on two machines.
        """
        source = Source(kind="file", location=FIXTURES_DIR / ".aider.chat.history.md")
        with pinned_tz("America/Chicago"):
            chicago = [c.external_id for c in aider.parse(source)]
        with pinned_tz("Asia/Tokyo"):
            tokyo = [c.external_id for c in aider.parse(source)]
        assert chicago == tokyo
        assert chicago[0].endswith("::2025-07-15 14:32:01")

    def test_parse_empty_and_header_only(self, tmp_path):
        empty = tmp_path / "e.md"
        empty.write_text("")
        assert list(aider.parse(Source(kind="file", location=empty))) == []
        header = tmp_path / "h.md"
        header.write_text("\n# aider chat started at 2025-01-01 00:00:00\n\n")
        assert list(aider.parse(Source(kind="file", location=header))) == []

    def test_parse_token_count_helper(self):
        for raw, expected in [("4.5k", 4500), ("1.2k", 1200), ("256", 256), ("1.5M", 1_500_000), ("bad", None)]:
            assert aider._parse_token_count(raw) == expected

    def test_discover(self, tmp_path):
        (tmp_path / ".aider.chat.history.md").write_text("# aider\n")
        assert list(aider.discover(locations=[str(tmp_path)]))
        assert list(aider.discover(locations=[str(tmp_path / "nope")])) == []

    def test_analytics_not_discovered(self, tmp_path):
        """Regression: analytics.jsonl must not appear in discover output (deferred until schema documented)."""
        d = tmp_path / ".aider"
        d.mkdir()
        (d / "analytics.jsonl").write_text('{"event": "test"}\n')
        (d / ".aider.chat.history.md").write_text("# aider chat started at 2025-01-01 00:00:00\n\n#### q\n\nA\n")
        sources = list(aider.discover(locations=[str(d)]))
        locs = [str(s.location) for s in sources]
        assert not any("analytics.jsonl" in loc for loc in locs)
        assert any(".aider.chat.history.md" in loc for loc in locs)
        assert not aider.can_handle(Source(kind="file", location=Path("/home/.aider/analytics.jsonl")))
        assert not aider.can_handle(Source(kind="file", location=Path("/random/analytics.jsonl")))

    def test_tool_before_response(self, tmp_path):
        """Tool output appearing before any assistant response creates a response with tool_output block."""
        md = "# aider chat started at 2025-01-01 00:00:00\n\n#### do it\n\n> Applied edit to main.py\n\nDone\n"
        (tmp_path / ".aider.chat.history.md").write_text(md)
        conv = list(aider.parse(Source(kind="file", location=tmp_path / ".aider.chat.history.md")))[0]
        assert conv.prompts[0].responses and any(b.block_type == "tool_output" for r in conv.prompts[0].responses for b in r.content)


# --- The decoder and the assembled shape ---------------------------------------

GOLDEN = FIXTURES_DIR / "adapters" / "aider"

_ONE_SESSION = "# aider chat started at 2025-01-01 00:00:00\n\n#### first\n\nA.\n"
_THREE_SESSIONS = (
    "# aider chat started at 2025-01-01 00:00:00\n\n#### one\n\nA.\n"
    "# aider chat started at 2025-01-02 00:00:00\n\n#### two\n\nB.\n"
    "# aider chat started at 2025-01-03 00:00:00\n\n#### three\n\nC.\n"
)

# One sample per unit kind the decoder table names.
_SAMPLE_UNITS = {
    "session": aider._Unit("session", stamp="2025-01-01 00:00:00"),
    "user": aider._Unit("user", text="q"),
    "assistant": aider._Unit("assistant", text="a"),
    "tool": aider._Unit("tool", text="Applied edit to x.py"),
    "tokens": aider._Unit("tokens", text="Tokens: 1k sent, 2 received."),
    "outside": aider._Unit("outside", text="banner"),
}


def _assembled(path):
    return assemble(
        Source(kind="file", location=path),
        read=aider.read_aider,
        decoder=aider.AiderDecoder,
        harness=aider.HARNESS,
    )


def _history(tmp_path, text, name="proj"):
    project = tmp_path / name
    project.mkdir(parents=True, exist_ok=True)
    path = project / ".aider.chat.history.md"
    path.write_text(text)
    return path


def _digest(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:12]


def _golden_input(case: str) -> Path:
    return next(p for p in (GOLDEN / case).iterdir() if p.name != "expected.json")


class TestDecodedShape:
    """What the decoder makes of each construct, and what the assembler does with it."""

    def test_the_decoder_table_names_every_unit_kind(self):
        """The decoder's docstring table is the unit ledger; it must not drift from what the code does."""
        rows = []
        for line in aider.AiderDecoder.__doc__.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.strip().startswith("| `") and len(cells) == 3:
                rows.append((re.match(r"`([^`]+)`", cells[0]).group(1), cells[1]))
        assert {kind for kind, _ in rows} == set(_SAMPLE_UNITS)
        ignored = {kind for kind, becomes in rows if becomes.startswith("Ignored")}
        assert ignored == {"outside"}
        source = Source(kind="file", location=Path("/x/.aider.chat.history.md"))
        for kind, unit in _SAMPLE_UNITS.items():
            records = aider.AiderDecoder(source).decode(unit)
            assert records and not any(isinstance(r, Unknown) for r in records), kind

    def test_a_banner_before_the_first_prompt_is_stored_not_dropped(self):
        """The old parser dropped every response and tool output before the first prompt (#93)."""
        assembly = _assembled(_golden_input("banner_turns"))
        (conversation, _) = assembly.conversations
        assert len(conversation.preamble) == 1
        assert "Aider v0.50.1" in conversation.preamble[0].content[0].content["text"]
        assert assembly.ledger.count(Disposition.ORPHAN) == 0

    def test_token_lines_are_usage_summed_onto_their_turn(self):
        """Each token line is one API call's usage. It sums onto the reply it follows, and it is not output text."""
        (conversation, _) = _assembled(_golden_input("banner_turns")).conversations
        first, second = conversation.prompts[0].responses
        assert first.usage == Usage(input_tokens=2100, output_tokens=256)
        assert second.usage == Usage(input_tokens=900, output_tokens=40)
        assert first.attributes == {"approx_cost": "0.01"}
        outputs = [b.content["text"] for r in conversation.prompts[0].responses for b in r.content if b.block_type == "tool_output"]
        assert outputs == ["Applied edit to src/fetch.py\nCommit 1a2b3c4 feat: retry fetch"]
        assert "approx_input_tokens" not in first.attributes

    def test_a_token_line_with_no_reply_before_it_holds_its_usage(self):
        """No reply in the prompt yet: the usage needs a response to belong to, so it holds an empty one."""
        (conversation,) = _assembled(_golden_input("tokens_before_reply")).conversations
        held, reply = conversation.prompts[0].responses
        assert held.usage == Usage(input_tokens=300, output_tokens=12)
        assert [b.block_type for b in held.content] == ["tool_output"]
        assert reply.content[0].content["text"] == "Tests pass."

    def test_one_file_one_id_however_the_scan_root_is_spelled(self, tmp_path, monkeypatch):
        """Relative and absolute spellings of one file were two ids, so one file could be stored twice."""
        path = _history(tmp_path, _ONE_SESSION)
        monkeypatch.chdir(tmp_path)
        relative = aider.parse(Source(kind="file", location=Path("proj/.aider.chat.history.md")))[0]
        absolute = aider.parse(Source(kind="file", location=path))[0]
        assert relative.external_id == absolute.external_id
        assert absolute.external_id == f"aider::{_digest(str(path.resolve()))}::2025-01-01 00:00:00"
        # The ids a previous release may have stored this file under are named by either spelling, so
        # ingest can adopt the row whichever way the file is scanned now. Each candidate is a spelling
        # relative to an ancestor of the file (the basename is the one a scan from its own directory
        # gave); ingest verifies a candidate against the stored messages before adopting it.
        legacy = {
            f"aider::{_digest('proj/.aider.chat.history.md')}::2025-01-01 00:00:00",
            f"aider::{_digest('.aider.chat.history.md')}::2025-01-01 00:00:00",
        }
        assert legacy <= set(relative.superseded_external_ids)
        assert set(relative.superseded_external_ids) == set(absolute.superseded_external_ids)
        assert absolute.external_id not in relative.superseded_external_ids

    def test_a_recorded_spelling_is_a_superseded_candidate(self, tmp_path):
        """A scan from a directory that is not an ancestor (`../proj`) names no ancestor spelling.

        The spelling the bookkeeping recorded is handed over by ingest in
        ``metadata["recorded_locations"]`` and becomes a candidate, so the row the
        previous release stored under it can be adopted. The current id never is.
        """
        path = _history(tmp_path, _ONE_SESSION)
        spelled = "../proj/.aider.chat.history.md"
        recorded = aider.parse(Source(kind="file", location=path, metadata={"recorded_locations": [spelled]}))[0]
        bare = aider.parse(Source(kind="file", location=path))[0]
        recorded_id = f"aider::{_digest(spelled)}::2025-01-01 00:00:00"
        assert recorded_id in recorded.superseded_external_ids
        assert recorded_id not in bare.superseded_external_ids
        assert recorded.external_id not in recorded.superseded_external_ids

    def test_a_source_stating_its_identity_path_is_keyed_by_it_verbatim(self):
        """Golden fixtures state their CWD-relative spelling, so their expected ids hold on every machine."""
        stated = "tests/fixtures/adapters/aider/minimal/.aider.chat.history.md"
        source = Source(kind="file", location=Path(stated), metadata={"identity_path": stated})
        (conversation,) = aider.parse(source)
        assert conversation.external_id == f"aider::{_digest(stated)}::2025-07-15 14:32:01"
        assert conversation.superseded_external_ids == ()

    def test_each_session_ends_where_the_next_begins_and_the_last_stays_open(self, tmp_path):
        """Aider states no end, so the only bound is the next header; the last session is still being written."""
        conversations = aider.parse(Source(kind="file", location=_history(tmp_path, _THREE_SESSIONS)))
        assert [c.ended_at for c in conversations] == [
            conversations[1].started_at,
            conversations[2].started_at,
            None,
        ]

    def test_an_exchange_appended_to_the_live_session_is_in_the_next_assembly(self, tmp_path):
        path = _history(tmp_path, _ONE_SESSION)
        before = _assembled(path).conversations[0]
        path.write_text(path.read_text() + "\n#### and add logging\n\nAdded.\n")
        after = _assembled(path).conversations[0]
        assert len(before.prompts) == 1
        assert len(after.prompts) == 2 and after.ended_at is None

    def test_every_construct_is_accounted_for(self):
        """Units are the file's constructs; every record has one disposition, and nothing is unknown or orphaned."""
        for case in sorted(p.name for p in GOLDEN.iterdir() if p.is_dir()):
            ledger = _assembled(_golden_input(case)).ledger
            assert ledger.units > 0, case
            assert ledger.disposed == ledger.records, case
            assert ledger.count(Disposition.UNKNOWN) == 0 and ledger.count(Disposition.ORPHAN) == 0, case

    def test_peek_shows_the_live_session_of_a_multi_session_file(self):
        path = GOLDEN / "multi_conversation" / ".aider.chat.history.md"
        scan = aider.peek_scan(path)
        assert scan.session_id.endswith("::2025-07-15 15:10:00")
        assert scan.exchange_count == 1
        (exchange,) = aider.peek_exchanges(path, 5)
        assert exchange.prompt_text == "fix the bug in auth.py"

    def test_peek_tail_is_the_last_raw_lines(self):
        path = GOLDEN / "minimal" / ".aider.chat.history.md"
        assert list(aider.peek_tail(path, 1)) == ["> Applied edit to hello.py"]

    def test_output_after_a_new_prompt_does_not_join_the_previous_turn(self, tmp_path):
        """A turn ends at the next prompt. Output that follows a prompt with no reply yet is that prompt's, not the last reply's."""
        path = _history(tmp_path, "# aider chat started at 2025-01-01 00:00:00\n\n#### one\n\nA.\n\n#### two\n\n> Applied edit to x.py\n")
        first, second = _assembled(path).conversations[0].prompts
        assert [r.content[0].content["text"] for r in first.responses] == ["A."]
        assert [b.block_type for r in second.responses for b in r.content] == ["tool_output"]

    def test_text_before_the_first_header_is_counted_not_discarded(self, tmp_path):
        """Nothing before a session header belongs to a session, but it is still the file's content: named, counted."""
        path = _history(tmp_path, "stray line\n\n" + _ONE_SESSION)
        assembly = _assembled(path)
        assert len(assembly.conversations) == 1
        assert assembly.ledger.reasons[(Disposition.IGNORED, "outside:before-first-session")] == 1
