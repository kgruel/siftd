"""Cross-format normalizer validation tests."""

import pytest
from conftest import ClaudeSession, CodexSession, CopilotSession, PiAgentSession

from siftd.adapters import claude_code, codex_cli, copilot_cli, pi_agent
from siftd.adapters.contract import adapt

# Every adapter with module-level peek hooks, all built from a decoder.
PEEK_ADAPTERS = [
    pytest.param(ClaudeSession, adapt(claude_code), id="claude_code"),
    pytest.param(PiAgentSession, adapt(pi_agent), id="pi_agent"),
    pytest.param(CopilotSession, adapt(copilot_cli), id="copilot_cli"),
    pytest.param(CodexSession, adapt(codex_cli), id="codex_cli"),
]


class TestMakePeekHooksIntegration:
    @pytest.mark.parametrize("builder_cls, adapter", PEEK_ADAPTERS)
    def test_generated_hooks(self, tmp_path, builder_cls, adapter):
        """Adapter's module-level peek_scan/peek_exchanges work correctly."""
        path = builder_cls(tmp_path, exchanges=3).build()
        assert adapter.peek_scan(path).exchange_count == 3
        assert len(adapter.peek_exchanges(path, 2)) == 2


class TestClaudeCodeSubagent:
    def test_subagent_detection(self, tmp_path):
        result = adapt(claude_code).peek_scan(ClaudeSession(tmp_path).with_subagent("sub-1").build())
        assert result.parent_session_id is not None and ":sub-1" in result.session_id
        result2 = adapt(claude_code).peek_scan(ClaudeSession(tmp_path, exchanges=1, name="main.jsonl").build())
        assert result2.parent_session_id is None
