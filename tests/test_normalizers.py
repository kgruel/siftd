"""Cross-format normalizer validation tests."""

import pytest
from conftest import ClaudeSession, CodexSession, CopilotSession, PiAgentSession

from siftd.adapters import claude_code, codex_cli, copilot_cli, pi_agent
from siftd.adapters.sdk import iter_jsonl, peek_exchanges_from_records, peek_scan_from_records

JSONL_ADAPTERS = [
    pytest.param(ClaudeSession, claude_code, id="claude_code"),
    pytest.param(CodexSession, codex_cli, id="codex_cli"),
    pytest.param(PiAgentSession, pi_agent, id="pi_agent"),
    pytest.param(CopilotSession, copilot_cli, id="copilot_cli"),
]


def _collect_normalized(path, adapter_module):
    return [nr for raw in iter_jsonl(path) if (nr := adapter_module.normalize_record(raw)) is not None]


class TestNormalizerContract:
    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_normalizer_produces_valid_records(self, tmp_path, builder_cls, adapter):
        """User/assistant records with timestamps and content for 1 and 3 exchanges."""
        for n in (1, 3):
            path = builder_cls(tmp_path, exchanges=n, name=f"s{n}.jsonl").build()
            records = _collect_normalized(path, adapter)
            kinds = {nr.kind for nr in records}
            assert "user" in kinds and "assistant" in kinds
            users = [nr for nr in records if nr.kind == "user"]
            assert len(users) == n
            for nr in records:
                if nr.kind in ("user", "assistant"):
                    assert nr.timestamp is not None
                    assert len(nr.content_blocks) > 0


class TestNormalizerPeekIntegration:
    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_peek_scan_and_exchanges(self, tmp_path, builder_cls, adapter):
        """peek_scan + peek_exchanges work correctly, including with tools."""
        # Basic scan
        path = builder_cls(tmp_path, exchanges=2).build()
        result = peek_scan_from_records(iter_jsonl(path), adapter.normalize_record, default_session_id=path.stem)
        assert result is not None and result.exchange_count == 2
        # With tools
        builder = builder_cls(tmp_path, exchanges=3, name="tools.jsonl")
        if hasattr(builder, "with_tools"):
            builder = builder.with_tools(["test_tool"])
        path2 = builder.build()
        assert peek_scan_from_records(iter_jsonl(path2), adapter.normalize_record, default_session_id=path2.stem).exchange_count == 3
        # Exchanges with text + last_n
        ex = peek_exchanges_from_records(iter_jsonl(path2), adapter.normalize_record, last_n=5)
        assert len(ex) == 3 and all(e.prompt_text and e.response_text for e in ex)
        ex2 = peek_exchanges_from_records(iter_jsonl(path2), adapter.normalize_record, last_n=2)
        assert len(ex2) == 2


class TestMakePeekHooksIntegration:
    @pytest.mark.parametrize("builder_cls, adapter", JSONL_ADAPTERS)
    def test_generated_hooks(self, tmp_path, builder_cls, adapter):
        """Adapter's module-level peek_scan/peek_exchanges work correctly."""
        path = builder_cls(tmp_path, exchanges=3).build()
        assert adapter.peek_scan(path).exchange_count == 3
        assert len(adapter.peek_exchanges(path, 2)) == 2


class TestClaudeCodeSubagent:
    def test_subagent_detection(self, tmp_path):
        result = claude_code.peek_scan(ClaudeSession(tmp_path).with_subagent("sub-1").build())
        assert result.parent_session_id is not None and ":sub-1" in result.session_id
        result2 = claude_code.peek_scan(ClaudeSession(tmp_path, exchanges=1, name="main.jsonl").build())
        assert result2.parent_session_id is None
