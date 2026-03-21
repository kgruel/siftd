"""Cross-format normalizer validation tests.

Generates equivalent sessions across all JSONL adapter formats using the
session builders, runs each adapter's normalize_record, and asserts the
NormalizedRecord streams are semantically equivalent.

This is the foundation for autoresearch cross-format integration testing.
"""

import pytest

from conftest import ClaudeSession, CodexSession, CopilotSession, PiAgentSession

from siftd.adapters import claude_code, codex_cli, copilot_cli, pi_agent
from siftd.adapters.sdk import (
    NormalizedRecord,
    iter_jsonl,
    make_peek_hooks,
    peek_exchanges_from_records,
    peek_scan_from_records,
)


# Each entry: (builder_class, adapter_module, description)
JSONL_ADAPTERS = [
    pytest.param(ClaudeSession, claude_code, id="claude_code"),
    pytest.param(CodexSession, codex_cli, id="codex_cli"),
    pytest.param(PiAgentSession, pi_agent, id="pi_agent"),
    pytest.param(CopilotSession, copilot_cli, id="copilot_cli"),
]


def _collect_normalized(path, adapter_module):
    """Run normalize_record on all JSONL records, return list of NormalizedRecords."""
    normalize = adapter_module.normalize_record
    results = []
    for raw in iter_jsonl(path):
        nr = normalize(raw)
        if nr is not None:
            results.append(nr)
    return results


class TestNormalizerContract:
    """All normalizers must satisfy the same contract."""

    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_normalizer_produces_user_and_assistant(self, tmp_path, builder_cls, adapter):
        """Every session with exchanges produces both user and assistant records."""
        path = builder_cls(tmp_path, exchanges=2).build()
        records = _collect_normalized(path, adapter)

        kinds = {nr.kind for nr in records}
        assert "user" in kinds, f"{adapter.NAME} normalizer missing 'user' records"
        assert "assistant" in kinds, f"{adapter.NAME} normalizer missing 'assistant' records"

    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_normalizer_user_count_matches_exchanges(self, tmp_path, builder_cls, adapter):
        """Number of 'user' NormalizedRecords matches the exchange count."""
        for n in (1, 3):
            path = builder_cls(tmp_path, exchanges=n, name=f"s{n}.jsonl").build()
            records = _collect_normalized(path, adapter)
            user_count = sum(1 for nr in records if nr.kind == "user")
            assert user_count == n, (
                f"{adapter.NAME}: expected {n} user records, got {user_count}"
            )

    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_normalizer_timestamps_present(self, tmp_path, builder_cls, adapter):
        """User and assistant records have timestamps."""
        path = builder_cls(tmp_path, exchanges=1).build()
        records = _collect_normalized(path, adapter)

        for nr in records:
            if nr.kind in ("user", "assistant"):
                assert nr.timestamp is not None, (
                    f"{adapter.NAME}: {nr.kind} record missing timestamp"
                )

    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_normalizer_user_has_content(self, tmp_path, builder_cls, adapter):
        """User records have non-empty content blocks."""
        path = builder_cls(tmp_path, exchanges=1).build()
        records = _collect_normalized(path, adapter)

        users = [nr for nr in records if nr.kind == "user"]
        assert len(users) >= 1
        for nr in users:
            assert len(nr.content_blocks) > 0, (
                f"{adapter.NAME}: user record has no content blocks"
            )

    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_normalizer_assistant_has_content(self, tmp_path, builder_cls, adapter):
        """Assistant records have non-empty content blocks."""
        path = builder_cls(tmp_path, exchanges=1).build()
        records = _collect_normalized(path, adapter)

        assistants = [nr for nr in records if nr.kind == "assistant"]
        assert len(assistants) >= 1
        for nr in assistants:
            assert len(nr.content_blocks) > 0, (
                f"{adapter.NAME}: assistant record has no content blocks"
            )


class TestNormalizerPeekIntegration:
    """Normalizers produce valid input for SDK peek functions."""

    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_peek_scan_succeeds(self, tmp_path, builder_cls, adapter):
        """peek_scan_from_records produces a result for every session."""
        path = builder_cls(tmp_path, exchanges=2).build()
        result = peek_scan_from_records(
            iter_jsonl(path),
            adapter.normalize_record,
            default_session_id=path.stem,
        )
        assert result is not None
        assert result.exchange_count == 2

    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_peek_scan_with_tools(self, tmp_path, builder_cls, adapter):
        """Sessions with tools still count the correct number of exchanges."""
        builder = builder_cls(tmp_path, exchanges=3)
        if hasattr(builder, "with_tools"):
            builder = builder.with_tools(["test_tool"])
        path = builder.build()
        result = peek_scan_from_records(
            iter_jsonl(path),
            adapter.normalize_record,
            default_session_id=path.stem,
        )
        assert result is not None
        assert result.exchange_count == 3

    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_peek_exchanges_succeeds(self, tmp_path, builder_cls, adapter):
        """peek_exchanges_from_records produces exchanges for every session."""
        path = builder_cls(tmp_path, exchanges=3).build()
        exchanges = peek_exchanges_from_records(
            iter_jsonl(path),
            adapter.normalize_record,
            last_n=5,
        )
        assert len(exchanges) == 3

    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_peek_exchanges_have_text(self, tmp_path, builder_cls, adapter):
        """Each exchange has both prompt and response text."""
        path = builder_cls(tmp_path, exchanges=2).build()
        exchanges = peek_exchanges_from_records(
            iter_jsonl(path),
            adapter.normalize_record,
            last_n=5,
        )
        for ex in exchanges:
            assert ex.prompt_text is not None, (
                f"{adapter.NAME}: exchange missing prompt_text"
            )
            assert ex.response_text is not None, (
                f"{adapter.NAME}: exchange missing response_text"
            )

    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_peek_exchanges_last_n(self, tmp_path, builder_cls, adapter):
        """last_n correctly limits the returned exchanges."""
        path = builder_cls(tmp_path, exchanges=5).build()
        exchanges = peek_exchanges_from_records(
            iter_jsonl(path),
            adapter.normalize_record,
            last_n=2,
        )
        assert len(exchanges) == 2


class TestMakePeekHooksIntegration:
    """make_peek_hooks-generated functions work correctly for each adapter."""

    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_generated_peek_scan(self, tmp_path, builder_cls, adapter):
        """Adapter's peek_scan (from make_peek_hooks) produces correct results."""
        path = builder_cls(tmp_path, exchanges=2).build()
        # Use the adapter's module-level peek_scan (generated by make_peek_hooks)
        result = adapter.peek_scan(path)
        assert result is not None
        assert result.exchange_count == 2

    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_generated_peek_exchanges(self, tmp_path, builder_cls, adapter):
        """Adapter's peek_exchanges (from make_peek_hooks) produces exchanges."""
        path = builder_cls(tmp_path, exchanges=3).build()
        exchanges = adapter.peek_exchanges(path, 5)
        assert len(exchanges) == 3


class TestClaudeCodeSubagent:
    """Claude Code-specific subagent detection through normalizer."""

    def test_subagent_via_agent_id(self, tmp_path):
        """Sessions with agentId are detected as subagents."""
        path = ClaudeSession(tmp_path).with_subagent("sub-1").build()
        result = claude_code.peek_scan(path)
        assert result is not None
        assert result.parent_session_id is not None
        assert ":sub-1" in result.session_id

    def test_main_session_has_no_parent(self, tmp_path):
        """Main sessions have no parent_session_id."""
        path = ClaudeSession(tmp_path, exchanges=1).build()
        result = claude_code.peek_scan(path)
        assert result is not None
        assert result.parent_session_id is None
