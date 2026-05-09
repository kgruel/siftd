"""Tests for the caveats producer registry and dispatch threading.

Slice 1 covers the registry mechanics and the pricing-missing producer.
The autouse `_reset_caveat_producers` fixture (in conftest) snapshots the
registry around each test so tests that register their own producers
don't leak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from painted import Fidelity

from siftd.api import caveats as caveats_mod
from siftd.api.caveats import (
    Caveat,
    ProducerContext,
    ProducerSpec,
    caveat_producer,
    run_producers,
)
from siftd.api.dispatch import Operation, dispatch, execute_for_render, render
from siftd.doctor.checks import Finding


@dataclass
class FakeSummary:
    id: str
    workspace_path: str | None
    model: str | None
    started_at: str | None
    prompt_count: int
    response_count: int
    total_tokens: int
    cost: float | None
    tags: list[str] = field(default_factory=list)


def _make_op(
    *,
    fn=None,
    render_method="list",
    fidelity=None,
    params=None,
    render_context=None,
):
    return Operation(
        path="/test",
        method="GET",
        fn=fn or (lambda: []),
        params=params or {},
        render_method=render_method,
        fidelity=fidelity or Fidelity(depth=3),
        db=Path("/test.db"),
        render_context=render_context or {},
    )


def _make_ctx() -> ProducerContext:
    """Minimal ProducerContext for tests that don't exercise the DB."""
    return ProducerContext(db_path=Path("/nonexistent.db"))


class TestCaveatAlias:
    def test_caveat_is_finding_alias(self):
        assert Caveat is Finding

    def test_finding_has_target_field(self):
        f = Finding(
            check="x", severity="info", message="m", fix_available=False, target="abc",
        )
        assert f.target == "abc"

    def test_target_defaults_to_none(self):
        f = Finding(check="x", severity="info", message="m", fix_available=False)
        assert f.target is None


class TestProducerRegistry:
    def test_decorator_registers_producer(self):
        @caveat_producer(kind="test-kind", applies_to=lambda op: True)
        def producer(op, result, ctx):
            return [Finding(check="test-kind", severity="info", message="m", fix_available=False)]

        op = _make_op()
        findings = run_producers(op, [], _make_ctx())
        assert any(f.check == "test-kind" for f in findings)

    def test_applies_to_false_skips_producer(self):
        called = []

        @caveat_producer(kind="never", applies_to=lambda op: False)
        def producer(op, result, ctx):
            called.append(True)
            return []

        run_producers(_make_op(), [], _make_ctx())
        assert called == []

    def test_producer_spec_shape(self):
        @caveat_producer(kind="shape-test", applies_to=lambda op: True)
        def producer(op, result, ctx):
            return []

        # Find the spec we just registered
        spec = next(s for s in caveats_mod._producers if s.kind == "shape-test")
        assert isinstance(spec, ProducerSpec)
        assert spec.kind == "shape-test"
        assert callable(spec.fn)
        assert callable(spec.applies_to)

    def test_isolation_fixture_restores_registry(self):
        """Verifies _reset_caveat_producers actually rolls back additions."""
        before = list(caveats_mod._producers)

        @caveat_producer(kind="leak-check", applies_to=lambda op: True)
        def producer(op, result, ctx):
            return []

        assert len(caveats_mod._producers) == len(before) + 1
        # Fixture restoration happens at teardown — verified across tests by
        # the absence of leak-check in subsequent runs.


class TestExecuteForRender:
    def test_returns_result_and_findings_tuple(self):
        @caveat_producer(kind="ef-test", applies_to=lambda op: True)
        def producer(op, result, ctx):
            return [Finding(
                check="ef-test", severity="info", message="m", fix_available=False,
            )]

        op = _make_op(fn=lambda: ["row1", "row2"])
        result, findings = execute_for_render(op)
        assert result == ["row1", "row2"]
        assert len(findings) == 1
        assert findings[0].check == "ef-test"

    def test_no_producers_returns_empty_findings(self):
        op = _make_op(fn=lambda: [])
        _, findings = execute_for_render(op)
        assert findings == []


class TestDispatchThreading:
    def test_render_threads_findings_into_render_context(self):
        """render() puts findings under 'caveats' in the kwargs passed to renderer."""
        seen_ctx: dict = {}

        class Fmt:
            def render_list(self, result, fidelity, **ctx):
                seen_ctx.update(ctx)
                return result

        op = _make_op(render_method="list", render_context={"detail_base": "/x"})
        f = Finding(check="t", severity="info", message="m", fix_available=False)
        render(["x"], op, fmt=Fmt(), findings=[f])

        assert "caveats" in seen_ctx
        assert seen_ctx["caveats"] == [f]
        assert seen_ctx["detail_base"] == "/x"

    def test_render_overwrites_pre_set_caveats_in_render_context(self):
        """Producer output is canonical — overwrites any pre-set ctx['caveats']."""
        seen_ctx: dict = {}

        class Fmt:
            def render_list(self, result, fidelity, **ctx):
                seen_ctx.update(ctx)
                return result

        op = _make_op(render_method="list", render_context={"caveats": ["pre-set"]})
        f = Finding(check="t", severity="info", message="m", fix_available=False)
        render(["x"], op, fmt=Fmt(), findings=[f])
        assert seen_ctx["caveats"] == [f]

    def test_render_no_findings_leaves_context_alone(self):
        seen_ctx: dict = {}

        class Fmt:
            def render_list(self, result, fidelity, **ctx):
                seen_ctx.update(ctx)
                return result

        op = _make_op(render_method="list", render_context={})
        render(["x"], op, fmt=Fmt(), findings=None)
        assert "caveats" not in seen_ctx

    def test_dispatch_runs_producers_and_renders(self):
        @caveat_producer(kind="d-test", applies_to=lambda op: True)
        def producer(op, result, ctx):
            return [Finding(
                check="d-test", severity="info", message="m", fix_available=False,
            )]

        seen: dict = {}

        class Fmt:
            def render_list(self, result, fidelity, **ctx):
                seen["result"] = result
                seen["caveats"] = ctx.get("caveats")
                return "rendered"

        op = _make_op(fn=lambda: ["a"], render_method="list")
        out = dispatch(op, fmt=Fmt())
        assert out == "rendered"
        assert seen["result"] == ["a"]
        assert len(seen["caveats"]) == 1
        assert seen["caveats"][0].check == "d-test"


class TestCapPolicy:
    """Cap logic in run_producers: infos capped at 3, hints at 1, errors/warnings uncapped."""

    def test_infos_capped_at_3_with_overflow(self):
        @caveat_producer(kind="info-cap-test", applies_to=lambda op: True)
        def producer(op, result, ctx):
            return [
                Finding(check="info-cap-test", severity="info", message=f"info {i}", fix_available=False)
                for i in range(4)
            ]

        findings = run_producers(_make_op(), [], _make_ctx())
        info_findings = [f for f in findings if f.check == "info-cap-test"]
        overflow = [f for f in findings if f.check == "findings-truncated"]
        assert len(info_findings) == 3
        assert len(overflow) == 1
        assert overflow[0].severity == "info"
        assert "+1 more" in overflow[0].message

    def test_overflow_message_pluralises(self):
        @caveat_producer(kind="info-plural-test", applies_to=lambda op: True)
        def producer(op, result, ctx):
            return [
                Finding(check="info-plural-test", severity="info", message=f"info {i}", fix_available=False)
                for i in range(6)
            ]

        findings = run_producers(_make_op(), [], _make_ctx())
        overflow = [f for f in findings if f.check == "findings-truncated"]
        assert len(overflow) == 1
        assert "+3 more" in overflow[0].message
        assert "findings" in overflow[0].message

    def test_hints_capped_at_1(self):
        @caveat_producer(kind="hint-cap-test", applies_to=lambda op: True)
        def producer(op, result, ctx):
            return [
                Finding(check="hint-cap-test", severity="hint", message=f"hint {i}", fix_available=False)
                for i in range(3)
            ]

        findings = run_producers(_make_op(), [], _make_ctx())
        hint_findings = [f for f in findings if f.check == "hint-cap-test"]
        assert len(hint_findings) == 1

    def test_warnings_uncapped(self):
        @caveat_producer(kind="warn-cap-test", applies_to=lambda op: True)
        def producer(op, result, ctx):
            return [
                Finding(check="warn-cap-test", severity="warning", message=f"w {i}", fix_available=False)
                for i in range(5)
            ]

        findings = run_producers(_make_op(), [], _make_ctx())
        assert len([f for f in findings if f.check == "warn-cap-test"]) == 5

    def test_errors_uncapped(self):
        @caveat_producer(kind="err-cap-test", applies_to=lambda op: True)
        def producer(op, result, ctx):
            return [
                Finding(check="err-cap-test", severity="error", message=f"e {i}", fix_available=False)
                for i in range(5)
            ]

        findings = run_producers(_make_op(), [], _make_ctx())
        assert len([f for f in findings if f.check == "err-cap-test"]) == 5

    def test_unknown_severity_passes_through_uncapped(self):
        @caveat_producer(kind="unknown-severity-test", applies_to=lambda op: True)
        def producer(op, result, ctx):
            return [
                Finding(
                    check="unknown-severity-test",
                    severity="unknown-future",
                    message="future severity should pass through",
                    fix_available=False,
                ),
            ]

        findings = run_producers(_make_op(), [], _make_ctx())
        unknown = [f for f in findings if f.check == "unknown-severity-test"]
        assert len(unknown) == 1
        assert unknown[0].severity == "unknown-future"

    def test_assembly_order(self):
        """errors → warnings → infos+overflow → hints."""
        @caveat_producer(kind="order-test", applies_to=lambda op: True)
        def producer(op, result, ctx):
            return [
                Finding(check="order-hint", severity="hint", message="h", fix_available=False),
                Finding(check="order-warning", severity="warning", message="w", fix_available=False),
                Finding(check="order-error", severity="error", message="e", fix_available=False),
                Finding(check="order-info", severity="info", message="i", fix_available=False),
            ]

        findings = run_producers(_make_op(), [], _make_ctx())
        severities = [f.severity for f in findings]
        # error before warning before info before hint
        error_idx = next(i for i, f in enumerate(findings) if f.check == "order-error")
        warning_idx = next(i for i, f in enumerate(findings) if f.check == "order-warning")
        info_idx = next(i for i, f in enumerate(findings) if f.check == "order-info")
        hint_idx = next(i for i, f in enumerate(findings) if f.check == "order-hint")
        assert error_idx < warning_idx < info_idx < hint_idx

    def test_no_overflow_when_exactly_at_cap(self):
        @caveat_producer(kind="exact-cap-test", applies_to=lambda op: True)
        def producer(op, result, ctx):
            return [
                Finding(check="exact-cap-test", severity="info", message=f"i{i}", fix_available=False)
                for i in range(3)
            ]

        findings = run_producers(_make_op(), [], _make_ctx())
        overflow = [f for f in findings if f.check == "findings-truncated"]
        assert overflow == []


class TestPricingProducer:
    """Tests for the pricing-missing producer.

    The producer's applies_to predicate is gated on (fn is list_conversations,
    render_method=='list', depth>=3). We verify the predicate, the empty-input
    short-circuit, and the row filtering.
    """

    def test_applies_to_requires_list_conversations(self):
        """Predicate is False for ops calling other functions."""
        from siftd.api.caveats import _is_list_conversations_at_depth

        op = _make_op(fn=lambda: [], fidelity=Fidelity(depth=3))
        assert _is_list_conversations_at_depth(op) is False

    def test_applies_to_requires_depth_3(self):
        from siftd.api.caveats import _is_list_conversations_at_depth
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, fidelity=Fidelity(depth=1))
        assert _is_list_conversations_at_depth(op) is False

    def test_applies_to_requires_render_method_list(self):
        from siftd.api.caveats import _is_list_conversations_at_depth
        from siftd.api.conversations import list_conversations

        op = _make_op(
            fn=list_conversations,
            render_method="detail",
            fidelity=Fidelity(depth=3),
        )
        assert _is_list_conversations_at_depth(op) is False

    def test_applies_to_satisfied(self):
        from siftd.api.caveats import _is_list_conversations_at_depth
        from siftd.api.conversations import list_conversations

        op = _make_op(
            fn=list_conversations,
            render_method="list",
            fidelity=Fidelity(depth=3),
        )
        assert _is_list_conversations_at_depth(op) is True

    def test_empty_summaries_short_circuits(self):
        from siftd.api.caveats import _pricing_caveats
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, fidelity=Fidelity(depth=3))
        assert _pricing_caveats(op, [], _make_ctx()) == []

    def test_all_rows_priced_short_circuits(self, monkeypatch):
        """If no row has cost=None, ctx.db() is never called."""
        def boom(*a, **kw):
            raise AssertionError("open_database should not be called")

        monkeypatch.setattr("siftd.api.database.open_database", boom)
        from siftd.api.caveats import _pricing_caveats
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, fidelity=Fidelity(depth=3))
        priced = [
            FakeSummary(
                id="01A", workspace_path=None, model="m", started_at=None,
                prompt_count=0, response_count=0, total_tokens=0, cost=0.05,
            ),
        ]
        assert _pricing_caveats(op, priced, _make_ctx()) == []

    def test_unpriced_row_produces_finding(self, monkeypatch):
        """When cost is None and the model is in the unpriced set, a Finding is produced."""
        from siftd.api.caveats import _pricing_caveats
        from siftd.api.conversations import list_conversations

        class FakeConn:
            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )
        monkeypatch.setattr(
            "siftd.storage.sqlite.get_models_without_pricing",
            lambda conn: [{"model_name": "claude-opus-4-7", "provider_name": "anthropic"}],
        )

        op = _make_op(fn=list_conversations, fidelity=Fidelity(depth=3))
        summaries = [
            FakeSummary(
                id="01A", workspace_path=None, model="claude-opus-4-7",
                started_at=None, prompt_count=0, response_count=0,
                total_tokens=0, cost=None,
            ),
            FakeSummary(
                id="02B", workspace_path=None, model="gpt-4o",
                started_at=None, prompt_count=0, response_count=0,
                total_tokens=0, cost=None,
            ),
        ]
        findings = _pricing_caveats(op, summaries, _make_ctx())
        assert len(findings) == 1
        assert findings[0].check == "pricing-missing"
        assert findings[0].target == "01A"
        assert findings[0].context == {"model": "claude-opus-4-7"}
        assert findings[0].fix_available is False

    def test_priced_row_skipped_even_if_model_unpriced(self, monkeypatch):
        """Row with computed cost is treated as priced regardless of pricing table state."""
        from siftd.api.caveats import _pricing_caveats
        from siftd.api.conversations import list_conversations

        class FakeConn:
            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )
        monkeypatch.setattr(
            "siftd.storage.sqlite.get_models_without_pricing",
            lambda conn: [{"model_name": "m1", "provider_name": "p"}],
        )

        op = _make_op(fn=list_conversations, fidelity=Fidelity(depth=3))
        summaries = [
            FakeSummary(
                id="01A", workspace_path=None, model="m1", started_at=None,
                prompt_count=0, response_count=0, total_tokens=0, cost=None,
            ),
            FakeSummary(
                id="02B", workspace_path=None, model="m1", started_at=None,
                prompt_count=0, response_count=0, total_tokens=0, cost=0.01,
            ),
        ]
        findings = _pricing_caveats(op, summaries, _make_ctx())
        assert len(findings) == 1
        assert findings[0].target == "01A"


class MockResult:
    """Helper for mocking SQLite cursor results."""

    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class TestWorkspaceIdentityProducer:
    """Tests for the workspace-identity producer.

    The producer checks if workspace_ids referenced by conversations have
    entries in the workspaces table. Unresolvable workspaces (orphaned refs)
    indicate that workspace filtering will not work correctly.
    """

    def test_applies_to_requires_list_conversations(self):
        """Predicate is False for ops calling other functions."""
        from siftd.api.caveats import _is_list_conversations_with_workspace

        op = _make_op(fn=lambda: [], fidelity=Fidelity(depth=2))
        assert _is_list_conversations_with_workspace(op) is False

    def test_applies_to_requires_depth_2(self):
        from siftd.api.caveats import _is_list_conversations_with_workspace
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, fidelity=Fidelity(depth=1))
        assert _is_list_conversations_with_workspace(op) is False

    def test_applies_to_requires_render_method_list(self):
        from siftd.api.caveats import _is_list_conversations_with_workspace
        from siftd.api.conversations import list_conversations

        op = _make_op(
            fn=list_conversations,
            render_method="detail",
            fidelity=Fidelity(depth=2),
        )
        assert _is_list_conversations_with_workspace(op) is False

    def test_applies_to_satisfied(self):
        from siftd.api.caveats import _is_list_conversations_with_workspace
        from siftd.api.conversations import list_conversations

        op = _make_op(
            fn=list_conversations,
            render_method="list",
            fidelity=Fidelity(depth=2),
        )
        assert _is_list_conversations_with_workspace(op) is True

    def test_empty_result_short_circuits(self):
        """Empty result returns empty list without DB call."""
        from siftd.api.caveats import _workspace_identity_caveats
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, fidelity=Fidelity(depth=2))
        assert _workspace_identity_caveats(op, [], _make_ctx()) == []

    def test_all_workspaces_resolved_no_finding(self, monkeypatch):
        """All workspace_ids have entries in workspaces table — no findings."""
        from siftd.api.caveats import _workspace_identity_caveats
        from siftd.api.conversations import list_conversations

        class FakeConn:
            def execute(self, sql, params=None):
                # First query: SELECT workspace_ids from conversations
                if "SELECT DISTINCT c.workspace_id" in sql:
                    return MockResult([
                        {"workspace_id": "ws-001"},
                        {"workspace_id": "ws-002"},
                    ])
                # Second query: SELECT ids from workspaces
                if "SELECT id FROM workspaces" in sql:
                    return MockResult([
                        {"id": "ws-001"},
                        {"id": "ws-002"},
                    ])
                return MockResult([])

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.caveats.ProducerContext.db",
            lambda self: FakeConn(),
        )

        op = _make_op(fn=list_conversations, fidelity=Fidelity(depth=2))
        summaries = [
            FakeSummary(
                id="01A", workspace_path="/home/user/proj1", model="m",
                started_at=None, prompt_count=0, response_count=0,
                total_tokens=0, cost=0.1,
            ),
            FakeSummary(
                id="02B", workspace_path="/home/user/proj2", model="m",
                started_at=None, prompt_count=0, response_count=0,
                total_tokens=0, cost=0.1,
            ),
        ]
        findings = _workspace_identity_caveats(op, summaries, _make_ctx())
        assert findings == []

    def test_unresolvable_workspace_produces_finding(self, monkeypatch):
        """Workspace_id with no entry in workspaces table produces a Finding."""
        from siftd.api.caveats import _workspace_identity_caveats
        from siftd.api.conversations import list_conversations

        class FakeConn:
            def execute(self, sql, params=None):
                # First query: SELECT workspace_ids from conversations
                if "SELECT DISTINCT c.workspace_id" in sql:
                    return MockResult([
                        {"workspace_id": "ws-001"},
                        {"workspace_id": "ws-orphaned"},
                    ])
                # Second query: SELECT ids from workspaces
                if "SELECT id FROM workspaces" in sql:
                    return MockResult([
                        {"id": "ws-001"},
                    ])
                return MockResult([])

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.caveats.ProducerContext.db",
            lambda self: FakeConn(),
        )

        op = _make_op(fn=list_conversations, fidelity=Fidelity(depth=2))
        summaries = [
            FakeSummary(
                id="01A", workspace_path="/home/user/proj1", model="m",
                started_at=None, prompt_count=0, response_count=0,
                total_tokens=0, cost=0.1,
            ),
            FakeSummary(
                id="02B", workspace_path=None, model="m",
                started_at=None, prompt_count=0, response_count=0,
                total_tokens=0, cost=0.1,
            ),
        ]
        findings = _workspace_identity_caveats(op, summaries, _make_ctx())
        assert len(findings) == 1
        assert findings[0].check == "workspace-identity"
        assert findings[0].severity == "info"
        assert findings[0].context == {"workspace_id": "ws-orphaned"}
        assert "ws-orph" in findings[0].message
        assert findings[0].fix_available is False

    def test_predicate_depth_gate(self):
        """Producer is not called when depth < 2."""
        from siftd.api.caveats import _is_list_conversations_with_workspace
        from siftd.api.conversations import list_conversations

        op = _make_op(
            fn=list_conversations,
            render_method="list",
            fidelity=Fidelity(depth=1),
        )
        assert _is_list_conversations_with_workspace(op) is False

    def test_multiple_unresolvable_workspaces(self, monkeypatch):
        """Multiple unresolvable workspace_ids produce multiple findings."""
        from siftd.api.caveats import _workspace_identity_caveats
        from siftd.api.conversations import list_conversations

        class FakeConn:
            def execute(self, sql, params=None):
                # First query: SELECT workspace_ids from conversations
                if "SELECT DISTINCT c.workspace_id" in sql:
                    return MockResult([
                        {"workspace_id": "ws-001"},
                        {"workspace_id": "ws-orphaned1"},
                        {"workspace_id": "ws-orphaned2"},
                    ])
                # Second query: SELECT ids from workspaces
                if "SELECT id FROM workspaces" in sql:
                    return MockResult([
                        {"id": "ws-001"},
                    ])
                return MockResult([])

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.caveats.ProducerContext.db",
            lambda self: FakeConn(),
        )

        op = _make_op(fn=list_conversations, fidelity=Fidelity(depth=2))
        summaries = [
            FakeSummary(
                id="01A", workspace_path="/home/user/proj1", model="m",
                started_at=None, prompt_count=0, response_count=0,
                total_tokens=0, cost=0.1,
            ),
        ]
        findings = _workspace_identity_caveats(op, summaries, _make_ctx())
        assert len(findings) == 2
        workspace_ids = {f.context["workspace_id"] for f in findings}
        assert workspace_ids == {"ws-orphaned1", "ws-orphaned2"}


class TestActiveSessionsProducer:
    """Tests for the active-sessions producer.

    The producer's applies_to predicate gates on (fn is list_conversations,
    render_method=='list', workspace filter present). The producer lazily
    imports peek layer and counts sessions not in the result set.
    """

    def test_predicate_requires_workspace_filter(self):
        """Predicate is False when workspace param is missing."""
        from siftd.api.caveats import _is_list_conversations_with_workspace_filter
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, render_method="list", params={})
        assert _is_list_conversations_with_workspace_filter(op) is False

    def test_predicate_requires_list_conversations(self):
        """Predicate is False for ops calling other functions."""
        from siftd.api.caveats import _is_list_conversations_with_workspace_filter

        op = _make_op(
            fn=lambda: [],
            render_method="list",
            params={"workspace": "proj"},
        )
        assert _is_list_conversations_with_workspace_filter(op) is False

    def test_predicate_requires_render_method_list(self):
        """Predicate is False when render_method is 'detail'."""
        from siftd.api.caveats import _is_list_conversations_with_workspace_filter
        from siftd.api.conversations import list_conversations

        op = _make_op(
            fn=list_conversations,
            render_method="detail",
            params={"workspace": "proj"},
        )
        assert _is_list_conversations_with_workspace_filter(op) is False

    def test_predicate_satisfied(self):
        """Predicate is True when all conditions met."""
        from siftd.api.caveats import _is_list_conversations_with_workspace_filter
        from siftd.api.conversations import list_conversations

        op = _make_op(
            fn=list_conversations,
            render_method="list",
            params={"workspace": "proj"},
        )
        assert _is_list_conversations_with_workspace_filter(op) is True

    def test_no_workspace_filter_short_circuits(self):
        """Op with no workspace param → no findings."""
        from siftd.api.caveats import _active_sessions_caveats
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, render_method="list", params={})
        assert _active_sessions_caveats(op, [], _make_ctx()) == []

    def test_empty_summaries_short_circuits(self):
        """Empty result set → no findings."""
        from siftd.api.caveats import _active_sessions_caveats
        from siftd.api.conversations import list_conversations

        op = _make_op(
            fn=list_conversations,
            render_method="list",
            params={"workspace": "proj"},
        )
        assert _active_sessions_caveats(op, [], _make_ctx()) == []

    def test_no_active_sessions_no_finding(self, monkeypatch):
        """No active sessions in workspace → no findings."""
        from siftd.api.caveats import _active_sessions_caveats
        from siftd.api.conversations import list_conversations

        monkeypatch.setattr("siftd.peek.list_active_sessions", lambda workspace: [])

        op = _make_op(
            fn=list_conversations,
            render_method="list",
            params={"workspace": "proj"},
        )
        summaries = [
            FakeSummary(
                id="01A", workspace_path=None, model="m", started_at=None,
                prompt_count=0, response_count=0, total_tokens=0, cost=None,
            ),
        ]
        assert _active_sessions_caveats(op, summaries, _make_ctx()) == []

    def test_active_sessions_produces_finding(self, monkeypatch):
        """Active sessions produce finding with count."""
        from siftd.api.caveats import _active_sessions_caveats
        from siftd.api.conversations import list_conversations
        from siftd.peek.types import SessionInfo
        from pathlib import Path

        def mock_list_active(workspace):
            return [
                SessionInfo(
                    session_id="session-001",
                    file_path=Path("/fake/session1.jsonl"),
                    workspace_path="/proj",
                    workspace_name="proj",
                    model="claude-sonnet-4-20250514",
                ),
                SessionInfo(
                    session_id="session-002",
                    file_path=Path("/fake/session2.jsonl"),
                    workspace_path="/proj",
                    workspace_name="proj",
                    model="claude-opus-4-7",
                ),
            ]

        monkeypatch.setattr("siftd.peek.list_active_sessions", mock_list_active)

        op = _make_op(
            fn=list_conversations,
            render_method="list",
            params={"workspace": "proj"},
        )
        summaries = [
            FakeSummary(
                id="01A", workspace_path=None, model="m", started_at=None,
                prompt_count=0, response_count=0, total_tokens=0, cost=None,
            ),
        ]
        findings = _active_sessions_caveats(op, summaries, _make_ctx())
        assert len(findings) == 1
        assert findings[0].check == "active-sessions"
        assert findings[0].severity == "info"
        assert findings[0].message == "2 active sessions in this workspace not yet ingested"
        assert findings[0].fix_available is True
        assert findings[0].fix_command == "siftd ingest"
        assert findings[0].context == {"count": 2, "workspace": "proj"}

    def test_active_sessions_singular_message(self, monkeypatch):
        """Message pluralizes correctly for 1 session."""
        from siftd.api.caveats import _active_sessions_caveats
        from siftd.api.conversations import list_conversations
        from siftd.peek.types import SessionInfo
        from pathlib import Path

        def mock_list_active(workspace):
            return [
                SessionInfo(
                    session_id="session-001",
                    file_path=Path("/fake/session1.jsonl"),
                    workspace_path="/proj",
                    workspace_name="proj",
                    model="claude-sonnet-4-20250514",
                ),
            ]

        monkeypatch.setattr("siftd.peek.list_active_sessions", mock_list_active)

        op = _make_op(
            fn=list_conversations,
            render_method="list",
            params={"workspace": "proj"},
        )
        summaries = [
            FakeSummary(
                id="01A", workspace_path=None, model="m", started_at=None,
                prompt_count=0, response_count=0, total_tokens=0, cost=None,
            ),
        ]
        findings = _active_sessions_caveats(op, summaries, _make_ctx())
        assert len(findings) == 1
        assert findings[0].message == "1 active session in this workspace not yet ingested"


class TestFreshCorpusProducer:
    """Tests for the fresh-corpus producer.

    The producer's applies_to predicate is gated on (fn is list_conversations,
    render_method=='list'). No depth gate — this caveat is useful at any depth.
    """

    def test_applies_to_requires_list_conversations(self):
        """Predicate is False for ops calling other functions."""
        from siftd.api.caveats import _is_list_conversations_list

        op = _make_op(fn=lambda: [], render_method="list")
        assert _is_list_conversations_list(op) is False

    def test_applies_to_requires_render_method_list(self):
        from siftd.api.caveats import _is_list_conversations_list
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, render_method="detail")
        assert _is_list_conversations_list(op) is False

    def test_applies_to_satisfied(self):
        from siftd.api.caveats import _is_list_conversations_list
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, render_method="list")
        assert _is_list_conversations_list(op) is True

    def test_applies_to_satisfied_at_depth_1(self):
        """Predicate should be true at any depth."""
        from siftd.api.caveats import _is_list_conversations_list
        from siftd.api.conversations import list_conversations

        op = _make_op(
            fn=list_conversations,
            render_method="list",
            fidelity=Fidelity(depth=1),
        )
        assert _is_list_conversations_list(op) is True

    def test_large_result_short_circuits(self, monkeypatch):
        """Result with 10+ items → no DB call, no finding."""
        def boom(*a, **kw):
            raise AssertionError("open_database should not be called")

        monkeypatch.setattr("siftd.api.database.open_database", boom)
        from siftd.api.caveats import _fresh_corpus_caveats
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations)
        result = ["item"] * 10
        assert _fresh_corpus_caveats(op, result, _make_ctx()) == []

    def test_small_corpus_produces_finding(self, monkeypatch, tmp_path):
        """Result < 10 items, corpus count < 10 → finding emitted."""
        from siftd.api.caveats import _fresh_corpus_caveats
        from siftd.api.conversations import list_conversations

        class FakeCursor:
            def execute(self, sql):
                return self

            def fetchone(self):
                return [5]

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )

        op = _make_op(fn=list_conversations)
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        result = ["item"] * 3
        findings = _fresh_corpus_caveats(op, result, ctx)
        assert len(findings) == 1
        assert findings[0].check == "fresh-corpus"
        assert findings[0].severity == "info"
        assert "5" in findings[0].message
        assert findings[0].context == {"total": 5}
        assert findings[0].fix_available is False

    def test_corpus_at_threshold_no_finding(self, monkeypatch, tmp_path):
        """Corpus with exactly 10 items → no finding."""
        from siftd.api.caveats import _fresh_corpus_caveats
        from siftd.api.conversations import list_conversations

        class FakeCursor:
            def execute(self, sql):
                return self

            def fetchone(self):
                return [10]

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )

        op = _make_op(fn=list_conversations)
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        result = ["item"] * 3
        findings = _fresh_corpus_caveats(op, result, ctx)
        assert findings == []

    def test_large_corpus_no_finding(self, monkeypatch, tmp_path):
        """Corpus with 100+ items → no finding even if result is small."""
        from siftd.api.caveats import _fresh_corpus_caveats
        from siftd.api.conversations import list_conversations

        class FakeCursor:
            def execute(self, sql):
                return self

            def fetchone(self):
                return [100]

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )

        op = _make_op(fn=list_conversations)
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        result = ["item"] * 2
        findings = _fresh_corpus_caveats(op, result, ctx)
        assert findings == []

    def test_singular_plural_message(self, monkeypatch, tmp_path):
        """Message uses 'conversation' singular for count=1."""
        from siftd.api.caveats import _fresh_corpus_caveats
        from siftd.api.conversations import list_conversations

        class FakeCursor:
            def execute(self, sql):
                return self

            def fetchone(self):
                return [1]

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )

        op = _make_op(fn=list_conversations)
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        result = ["item"]
        findings = _fresh_corpus_caveats(op, result, ctx)
        assert len(findings) == 1
        assert "1 conversation" in findings[0].message
        assert "conversations" not in findings[0].message

    def test_nonexistent_db_returns_empty(self):
        """Nonexistent database path → no DB call, no finding."""
        from siftd.api.caveats import _fresh_corpus_caveats
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations)
        ctx = ProducerContext(db_path=Path("/nonexistent/path/db.sqlite"))
        result = ["item"] * 3
        findings = _fresh_corpus_caveats(op, result, ctx)
        assert findings == []

class TestEmbeddingsStaleProducer:
    """Tests for the embeddings-stale producer.

    The producer's applies_to predicate is gated on (fn is search_chunks,
    render_method=='search'). The producer checks embeddings availability,
    opens the embeddings db separately, and compares indexed conversation IDs.
    """

    def test_applies_to_requires_search_chunks(self):
        """Predicate is False for ops calling other functions."""
        from siftd.api.caveats import _is_search_chunks_for_search_render

        op = _make_op(fn=lambda: [], render_method="search")
        assert _is_search_chunks_for_search_render(op) is False

    def test_applies_to_requires_search_render_method(self):
        from siftd.api.caveats import _is_search_chunks_for_search_render
        from siftd.api.search import search_chunks

        op = _make_op(fn=search_chunks, render_method="list")
        assert _is_search_chunks_for_search_render(op) is False

    def test_applies_to_satisfied(self):
        from siftd.api.caveats import _is_search_chunks_for_search_render
        from siftd.api.search import search_chunks

        op = _make_op(fn=search_chunks, render_method="search")
        assert _is_search_chunks_for_search_render(op) is True

    def test_embeddings_unavailable_short_circuits(self, monkeypatch):
        """If embeddings_available() is False, no DB work occurs."""
        from siftd.api.caveats import _embeddings_stale_caveats
        from siftd.api.search import search_chunks

        monkeypatch.setattr(
            "siftd.embeddings.availability.embeddings_available", lambda: False
        )

        op = _make_op(fn=search_chunks, render_method="search")
        assert _embeddings_stale_caveats(op, [], _make_ctx()) == []

    def test_embed_db_missing_returns_empty(self, monkeypatch, tmp_path):
        """If embed db path doesn't exist, return empty list (no error)."""
        from siftd.api.caveats import _embeddings_stale_caveats
        from siftd.api.search import search_chunks

        monkeypatch.setattr(
            "siftd.embeddings.availability.embeddings_available", lambda: True
        )
        monkeypatch.setattr(
            "siftd.paths.embeddings_db_path",
            lambda: "/nonexistent/embed.db",
        )

        db_file = tmp_path / "main.db"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        op = _make_op(fn=search_chunks, render_method="search")
        assert _embeddings_stale_caveats(op, [], ctx) == []

    def test_main_db_missing_returns_empty(self, monkeypatch, tmp_path):
        """If main db path doesn't exist, return empty list (no error)."""
        from siftd.api.caveats import _embeddings_stale_caveats
        from siftd.api.search import search_chunks

        monkeypatch.setattr(
            "siftd.embeddings.availability.embeddings_available", lambda: True
        )
        embed_file = tmp_path / "embed.db"
        embed_file.touch()
        monkeypatch.setattr(
            "siftd.paths.embeddings_db_path",
            lambda: str(embed_file),
        )

        ctx = ProducerContext(db_path="/nonexistent/main.db")
        op = _make_op(fn=search_chunks, render_method="search")
        assert _embeddings_stale_caveats(op, [], ctx) == []

    @pytest.mark.embeddings
    def test_all_indexed_no_finding(self, monkeypatch, tmp_path):
        """All conversations indexed → no finding."""
        from siftd.api.caveats import _embeddings_stale_caveats
        from siftd.api.search import search_chunks

        # Create dummy DB files
        db_file = tmp_path / "main.db"
        db_file.touch()
        embed_file = tmp_path / "embed.db"
        embed_file.touch()

        monkeypatch.setattr(
            "siftd.embeddings.availability.embeddings_available", lambda: True
        )
        monkeypatch.setattr(
            "siftd.paths.embeddings_db_path", lambda: str(embed_file)
        )

        # Mock indexed_ids to return the same set as main IDs
        indexed_ids_set = {"conv-001", "conv-002"}
        monkeypatch.setattr(
            "siftd.storage.embeddings.get_indexed_conversation_ids",
            lambda conn: indexed_ids_set,
        )

        class FakeConn:
            def execute(self, sql, params=None):
                return self

            def fetchall(self):
                return [("conv-001",), ("conv-002",)]

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn()
        )
        monkeypatch.setattr(
            "siftd.api.caveats.ProducerContext.db",
            lambda self: FakeConn(),
        )

        ctx = ProducerContext(db_path=db_file)
        op = _make_op(fn=search_chunks, render_method="search")
        findings = _embeddings_stale_caveats(op, [], ctx)
        assert findings == []

    @pytest.mark.embeddings
    def test_missing_conversations_produces_warning(self, monkeypatch, tmp_path):
        """Missing conversations produce warning with count and fix command."""
        from siftd.api.caveats import _embeddings_stale_caveats
        from siftd.api.search import search_chunks

        # Create dummy DB files
        db_file = tmp_path / "main.db"
        db_file.touch()
        embed_file = tmp_path / "embed.db"
        embed_file.touch()

        monkeypatch.setattr(
            "siftd.embeddings.availability.embeddings_available", lambda: True
        )
        monkeypatch.setattr(
            "siftd.paths.embeddings_db_path", lambda: str(embed_file)
        )

        # Mock indexed_ids to return only 1 conversation
        indexed_ids_set = {"conv-001"}
        monkeypatch.setattr(
            "siftd.storage.embeddings.get_indexed_conversation_ids",
            lambda conn: indexed_ids_set,
        )

        class FakeConn:
            def execute(self, sql, params=None):
                return self

            def fetchall(self):
                # Return 3 conversations in main db
                return [("conv-001",), ("conv-002",), ("conv-003",)]

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn()
        )
        monkeypatch.setattr(
            "siftd.api.caveats.ProducerContext.db",
            lambda self: FakeConn(),
        )

        ctx = ProducerContext(db_path=db_file)
        op = _make_op(fn=search_chunks, render_method="search")
        findings = _embeddings_stale_caveats(op, [], ctx)
        assert len(findings) == 1
        assert findings[0].check == "embeddings-stale"
        assert findings[0].severity == "warning"
        assert findings[0].fix_available is True
        assert findings[0].fix_command == "siftd search --index"
        assert findings[0].context == {"count": 2}
        assert "2 conversations not indexed" in findings[0].message

    @pytest.mark.embeddings
    def test_singular_conversation_message(self, monkeypatch, tmp_path):
        """Message uses singular 'conversation' when count=1."""
        from siftd.api.caveats import _embeddings_stale_caveats
        from siftd.api.search import search_chunks

        db_file = tmp_path / "main.db"
        db_file.touch()
        embed_file = tmp_path / "embed.db"
        embed_file.touch()

        monkeypatch.setattr(
            "siftd.embeddings.availability.embeddings_available", lambda: True
        )
        monkeypatch.setattr(
            "siftd.paths.embeddings_db_path", lambda: str(embed_file)
        )

        # Mock indexed_ids to return empty set
        monkeypatch.setattr(
            "siftd.storage.embeddings.get_indexed_conversation_ids",
            lambda conn: set(),
        )

        class FakeConn:
            def execute(self, sql, params=None):
                return self

            def fetchall(self):
                # Return 1 conversation in main db
                return [("conv-001",)]

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn()
        )
        monkeypatch.setattr(
            "siftd.api.caveats.ProducerContext.db",
            lambda self: FakeConn(),
        )

        ctx = ProducerContext(db_path=db_file)
        op = _make_op(fn=search_chunks, render_method="search")
        findings = _embeddings_stale_caveats(op, [], ctx)
        assert len(findings) == 1
        assert "1 conversation not indexed" in findings[0].message
        assert "conversations" not in findings[0].message.split("not indexed")[0]

    def test_predicate_false_for_list_render_method(self):
        """Predicate is False when render_method is 'list'."""
        from siftd.api.caveats import _is_search_chunks_for_search_render
        from siftd.api.search import search_chunks

        op = _make_op(fn=search_chunks, render_method="list")
        assert _is_search_chunks_for_search_render(op) is False



class TestPendingTagsProducer:
    """Tests for the pending-tags producer.

    The producer's applies_to predicate is gated on (fn is list_conversations,
    render_method=='list'). The producer checks pending_tags table count and
    emits an info finding with fix_command if count > 0.
    """

    def test_applies_to_requires_list_conversations(self):
        """Predicate is False for ops calling other functions."""
        from siftd.api.caveats import _is_list_conversations_list

        op = _make_op(fn=lambda: [], render_method="list")
        assert _is_list_conversations_list(op) is False

    def test_applies_to_requires_render_method_list(self):
        from siftd.api.caveats import _is_list_conversations_list
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, render_method="detail")
        assert _is_list_conversations_list(op) is False

    def test_applies_to_satisfied(self):
        from siftd.api.caveats import _is_list_conversations_list
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, render_method="list")
        assert _is_list_conversations_list(op) is True

    def test_nonexistent_db_returns_empty(self):
        """Path(ctx.db_path) doesn't exist → no finding."""
        from siftd.api.caveats import _pending_tags_caveats
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations)
        ctx = ProducerContext(db_path=Path("/nonexistent/path/db.sqlite"))
        assert _pending_tags_caveats(op, [], ctx) == []

    def test_no_pending_tags_no_finding(self, monkeypatch):
        """mock COUNT returns 0 → no finding."""
        from siftd.api.caveats import _pending_tags_caveats
        from siftd.api.conversations import list_conversations

        class FakeConn:
            def execute(self, sql):
                return self

            def fetchone(self):
                return [0]

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )

        op = _make_op(fn=list_conversations)
        ctx = ProducerContext(db_path=Path("/exists.db"))
        assert _pending_tags_caveats(op, [], ctx) == []

    def test_pending_tags_produces_finding(self, monkeypatch, tmp_path):
        """mock COUNT returns 3 → info finding, count=3, fix_command="siftd ingest"."""
        from siftd.api.caveats import _pending_tags_caveats
        from siftd.api.conversations import list_conversations

        class FakeConn:
            def execute(self, sql):
                return self

            def fetchone(self):
                return [3]

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )

        op = _make_op(fn=list_conversations)
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        findings = _pending_tags_caveats(op, [], ctx)
        assert len(findings) == 1
        assert findings[0].check == "pending-tags"
        assert findings[0].severity == "info"
        assert findings[0].message == "3 pending tag intents — run 'siftd ingest' to apply"
        assert findings[0].fix_available is True
        assert findings[0].fix_command == "siftd ingest"
        assert findings[0].context == {"count": 3}

    def test_singular_message(self, monkeypatch, tmp_path):
        """count=1 → message uses singular "intent" not "intents"."""
        from siftd.api.caveats import _pending_tags_caveats
        from siftd.api.conversations import list_conversations

        class FakeConn:
            def execute(self, sql):
                return self

            def fetchone(self):
                return [1]

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )

        op = _make_op(fn=list_conversations)
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        findings = _pending_tags_caveats(op, [], ctx)
        assert len(findings) == 1
        assert findings[0].message == "1 pending tag intent — run 'siftd ingest' to apply"

class TestIngestStatusProducer:
    """Tests for the ingest-status producer.

    The producer emits findings about ingestion state: errors in ingested_files
    (warning), never-ingested (info), or stale last-ingest (info).
    The applies_to predicate is gated on (fn is list_conversations,
    render_method=='list') — fires at any depth or filter state.
    """

    def test_applies_to_requires_list_conversations(self):
        """Predicate is False for ops calling other functions."""
        from siftd.api.caveats import _is_list_conversations_list

        op = _make_op(fn=lambda: [], render_method="list")
        assert _is_list_conversations_list(op) is False

    def test_applies_to_requires_render_method_list(self):
        from siftd.api.caveats import _is_list_conversations_list
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, render_method="detail")
        assert _is_list_conversations_list(op) is False

    def test_applies_to_satisfied(self):
        from siftd.api.caveats import _is_list_conversations_list
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, render_method="list")
        assert _is_list_conversations_list(op) is True

    def test_nonexistent_db_returns_empty(self):
        """Path doesn't exist → no findings."""
        from siftd.api.caveats import _ingest_status_caveats
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations)
        ctx = ProducerContext(db_path=Path("/nonexistent/path/db.sqlite"))
        findings = _ingest_status_caveats(op, [], ctx)
        assert findings == []

    def test_no_errors_no_stale_returns_empty(self, monkeypatch):
        """No errors, recent ingest → no findings."""
        from siftd.api.caveats import _ingest_status_caveats
        from siftd.api.conversations import list_conversations

        class FakeConn:
            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )
        monkeypatch.setattr(
            "siftd.storage.sqlite.get_ingest_errors",
            lambda conn: [],
        )
        monkeypatch.setattr(
            "siftd.storage.queries.fetch_last_ingest_time",
            lambda conn: "2026-05-07T10:00:00+00:00",
        )

        op = _make_op(fn=list_conversations)
        findings = _ingest_status_caveats(op, [], _make_ctx())
        assert findings == []

    def test_ingest_errors_produce_warning(self, monkeypatch, tmp_path):
        """Ingest errors → warning finding with count."""
        from siftd.api.caveats import _ingest_status_caveats
        from siftd.api.conversations import list_conversations

        class FakeConn:
            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )
        monkeypatch.setattr(
            "siftd.storage.sqlite.get_ingest_errors",
            lambda conn: [
                {"path": "/f1.json", "error": "parse error", "harness_name": "claude-code"},
                {"path": "/f2.json", "error": "io error", "harness_name": "aider"},
            ],
        )
        monkeypatch.setattr(
            "siftd.storage.queries.fetch_last_ingest_time",
            lambda conn: "2026-05-07T10:00:00+00:00",
        )

        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        op = _make_op(fn=list_conversations)
        findings = _ingest_status_caveats(op, [], ctx)
        assert len(findings) == 1
        assert findings[0].check == "ingest-errors"
        assert findings[0].severity == "warning"
        assert findings[0].message == "2 files failed ingestion — run 'siftd doctor' for details"
        assert findings[0].fix_available is True
        assert findings[0].fix_command == "siftd doctor"
        assert findings[0].context == {"count": 2}

    def test_ingest_errors_singular_message(self, monkeypatch, tmp_path):
        """Ingest errors with count=1 → singular message."""
        from siftd.api.caveats import _ingest_status_caveats
        from siftd.api.conversations import list_conversations

        class FakeConn:
            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )
        monkeypatch.setattr(
            "siftd.storage.sqlite.get_ingest_errors",
            lambda conn: [{"path": "/f1.json", "error": "parse error", "harness_name": "claude-code"}],
        )
        monkeypatch.setattr(
            "siftd.storage.queries.fetch_last_ingest_time",
            lambda conn: "2026-05-07T10:00:00+00:00",
        )

        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        op = _make_op(fn=list_conversations)
        findings = _ingest_status_caveats(op, [], ctx)
        assert len(findings) == 1
        assert "1 file failed" in findings[0].message

    def test_never_ingested_produces_info(self, monkeypatch, tmp_path):
        """No ingest recorded → info finding check='ingest-never-run'."""
        from siftd.api.caveats import _ingest_status_caveats
        from siftd.api.conversations import list_conversations

        class FakeConn:
            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )
        monkeypatch.setattr(
            "siftd.storage.sqlite.get_ingest_errors",
            lambda conn: [],
        )
        monkeypatch.setattr(
            "siftd.storage.queries.fetch_last_ingest_time",
            lambda conn: None,
        )

        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        op = _make_op(fn=list_conversations)
        findings = _ingest_status_caveats(op, [], ctx)
        assert len(findings) == 1
        assert findings[0].check == "ingest-never-run"
        assert findings[0].severity == "info"
        assert "No ingest recorded" in findings[0].message
        assert findings[0].fix_available is True
        assert findings[0].fix_command == "siftd ingest"

    def test_stale_ingest_produces_info(self, monkeypatch, tmp_path):
        """Last ingest > 7 days ago → info finding check='ingest-stale', age_days=10."""
        from siftd.api.caveats import _ingest_status_caveats
        from siftd.api.conversations import list_conversations
        from datetime import datetime, timezone, timedelta

        class FakeConn:
            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )
        monkeypatch.setattr(
            "siftd.storage.sqlite.get_ingest_errors",
            lambda conn: [],
        )
        # 10 days ago
        ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        monkeypatch.setattr(
            "siftd.storage.queries.fetch_last_ingest_time",
            lambda conn: ten_days_ago,
        )

        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        op = _make_op(fn=list_conversations)
        findings = _ingest_status_caveats(op, [], ctx)
        assert len(findings) == 1
        assert findings[0].check == "ingest-stale"
        assert findings[0].severity == "info"
        assert "10 days ago" in findings[0].message
        assert findings[0].fix_available is True
        assert findings[0].fix_command == "siftd ingest"
        assert findings[0].context == {"age_days": 10}

    def test_fresh_ingest_no_stale_finding(self, monkeypatch, tmp_path):
        """Last ingest 3 days ago → no stale finding."""
        from siftd.api.caveats import _ingest_status_caveats
        from siftd.api.conversations import list_conversations
        from datetime import datetime, timezone, timedelta

        class FakeConn:
            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )
        monkeypatch.setattr(
            "siftd.storage.sqlite.get_ingest_errors",
            lambda conn: [],
        )
        # 3 days ago
        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        monkeypatch.setattr(
            "siftd.storage.queries.fetch_last_ingest_time",
            lambda conn: three_days_ago,
        )

        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        op = _make_op(fn=list_conversations)
        findings = _ingest_status_caveats(op, [], ctx)
        assert findings == []

    def test_multiple_findings_together(self, monkeypatch, tmp_path):
        """Errors + stale → both findings emitted."""
        from siftd.api.caveats import _ingest_status_caveats
        from siftd.api.conversations import list_conversations
        from datetime import datetime, timezone, timedelta

        class FakeConn:
            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )
        monkeypatch.setattr(
            "siftd.storage.sqlite.get_ingest_errors",
            lambda conn: [{"path": "/f1.json", "error": "error", "harness_name": "claude-code"}],
        )
        # 10 days ago
        ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        monkeypatch.setattr(
            "siftd.storage.queries.fetch_last_ingest_time",
            lambda conn: ten_days_ago,
        )

        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        op = _make_op(fn=list_conversations)
        findings = _ingest_status_caveats(op, [], ctx)
        assert len(findings) == 2
        checks = {f.check for f in findings}
        assert checks == {"ingest-errors", "ingest-stale"}
        assert findings[0].severity == "warning"  # Errors first
        assert findings[1].severity == "info"  # Then staleness


class TestFtsStaleProducer:
    """Tests for the fts-stale producer.

    The producer's applies_to predicate gates on (fn is search_chunks).
    It checks FTS index sync status and warns if missing or orphaned entries exist.
    """

    def test_applies_to_requires_search_chunks(self):
        """Predicate is False for ops calling other functions."""
        from siftd.api.caveats import _is_search_chunks

        op = _make_op(fn=lambda: [])
        assert _is_search_chunks(op) is False

    def test_applies_to_satisfied(self):
        """Predicate is True when op.fn is search_chunks."""
        from siftd.api.caveats import _is_search_chunks
        from siftd.api.search import search_chunks

        op = _make_op(fn=search_chunks)
        assert _is_search_chunks(op) is True

    def test_nonexistent_db_returns_empty(self):
        """Path doesn't exist → no finding."""
        from siftd.api.caveats import _fts_stale_caveats
        from siftd.api.search import search_chunks

        op = _make_op(fn=search_chunks)
        ctx = ProducerContext(db_path=Path("/nonexistent/path/db.sqlite"))
        findings = _fts_stale_caveats(op, [], ctx)
        assert findings == []

    def test_clean_index_no_finding(self, monkeypatch):
        """No missing or orphaned entries → no finding."""
        from siftd.api.caveats import _fts_stale_caveats
        from siftd.api.search import search_chunks

        monkeypatch.setattr(
            "siftd.storage.fts.get_fts_sync_status",
            lambda conn: {"missing_count": 0, "orphaned_count": 0},
        )

        op = _make_op(fn=search_chunks)
        findings = _fts_stale_caveats(op, [], _make_ctx())
        assert findings == []

    def test_missing_content_produces_warning(self, monkeypatch, tmp_path):
        """Missing content blocks → warning with fix_command."""
        from siftd.api.caveats import _fts_stale_caveats
        from siftd.api.search import search_chunks

        monkeypatch.setattr(
            "siftd.storage.fts.get_fts_sync_status",
            lambda conn: {"missing_count": 3, "orphaned_count": 0},
        )

        op = _make_op(fn=search_chunks)
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        findings = _fts_stale_caveats(op, [], ctx)
        assert len(findings) == 1
        assert findings[0].check == "fts-stale"
        assert findings[0].severity == "warning"
        assert "3 content blocks not indexed" in findings[0].message
        assert findings[0].fix_available is True
        assert findings[0].fix_command == "siftd db vacuum"
        assert findings[0].context == {"missing_count": 3, "orphaned_count": 0}

    def test_orphaned_entries_produces_warning(self, monkeypatch, tmp_path):
        """Orphaned FTS entries → warning with fix_command."""
        from siftd.api.caveats import _fts_stale_caveats
        from siftd.api.search import search_chunks

        monkeypatch.setattr(
            "siftd.storage.fts.get_fts_sync_status",
            lambda conn: {"missing_count": 0, "orphaned_count": 2},
        )

        op = _make_op(fn=search_chunks)
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        findings = _fts_stale_caveats(op, [], ctx)
        assert len(findings) == 1
        assert findings[0].check == "fts-stale"
        assert findings[0].severity == "warning"
        assert "2 orphaned FTS entries" in findings[0].message
        assert findings[0].fix_available is True
        assert findings[0].fix_command == "siftd db vacuum"
        assert findings[0].context == {"missing_count": 0, "orphaned_count": 2}

    def test_both_missing_and_orphaned(self, monkeypatch, tmp_path):
        """Both missing and orphaned → single warning mentioning both."""
        from siftd.api.caveats import _fts_stale_caveats
        from siftd.api.search import search_chunks

        monkeypatch.setattr(
            "siftd.storage.fts.get_fts_sync_status",
            lambda conn: {"missing_count": 3, "orphaned_count": 2},
        )

        op = _make_op(fn=search_chunks)
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        findings = _fts_stale_caveats(op, [], ctx)
        assert len(findings) == 1
        assert findings[0].check == "fts-stale"
        assert findings[0].severity == "warning"
        assert "3 content blocks not indexed" in findings[0].message
        assert "2 orphaned FTS entries" in findings[0].message
        assert findings[0].fix_available is True
        assert findings[0].fix_command == "siftd db vacuum"
        assert findings[0].context == {"missing_count": 3, "orphaned_count": 2}

    def test_singular_missing_message(self, monkeypatch, tmp_path):
        """Message uses singular 'block' for missing_count=1."""
        from siftd.api.caveats import _fts_stale_caveats
        from siftd.api.search import search_chunks

        monkeypatch.setattr(
            "siftd.storage.fts.get_fts_sync_status",
            lambda conn: {"missing_count": 1, "orphaned_count": 0},
        )

        op = _make_op(fn=search_chunks)
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        findings = _fts_stale_caveats(op, [], ctx)
        assert len(findings) == 1
        assert "1 content block not indexed" in findings[0].message

    def test_singular_orphaned_message(self, monkeypatch, tmp_path):
        """Message uses singular 'entry' for orphaned_count=1."""
        from siftd.api.caveats import _fts_stale_caveats
        from siftd.api.search import search_chunks

        monkeypatch.setattr(
            "siftd.storage.fts.get_fts_sync_status",
            lambda conn: {"missing_count": 0, "orphaned_count": 1},
        )

        op = _make_op(fn=search_chunks)
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        findings = _fts_stale_caveats(op, [], ctx)
        assert len(findings) == 1
        assert "1 orphaned FTS entry" in findings[0].message


class TestSearchModeDegradedProducer:
    """Tests for the search-mode-degraded producer (B8).

    Producer fires when op.params["mode"] == "fts" and result is non-empty.
    Predicate is shared with embeddings-stale: fn is search_chunks + render_method=="search".
    """

    def test_applies_to_predicate(self):
        """Predicate matches search_chunks + render_method=='search'."""
        from siftd.api.caveats import _is_search_chunks_for_search_render
        from siftd.api.search import search_chunks

        op_match = _make_op(fn=search_chunks, render_method="search")
        assert _is_search_chunks_for_search_render(op_match) is True

        op_wrong_method = _make_op(fn=search_chunks, render_method="list")
        assert _is_search_chunks_for_search_render(op_wrong_method) is False

    def test_empty_result_no_finding(self):
        """Empty result → no finding."""
        from siftd.api.caveats import _search_mode_degraded_caveats
        from siftd.api.search import search_chunks

        op = _make_op(fn=search_chunks, render_method="search", params={"mode": "fts"})
        findings = _search_mode_degraded_caveats(op, [], _make_ctx())
        assert findings == []

    def test_non_fts_mode_no_finding(self):
        """mode='hybrid' → no finding even with results."""
        from siftd.api.caveats import _search_mode_degraded_caveats
        from siftd.api.search import search_chunks
        from siftd.domain.search_types import SearchChunk

        chunk = SearchChunk(conversation_id="abc", score=0.9, text="x", chunk_type="prompt")
        op = _make_op(fn=search_chunks, render_method="search", params={"mode": "hybrid"})
        findings = _search_mode_degraded_caveats(op, [chunk], _make_ctx())
        assert findings == []

    def test_missing_mode_param_no_finding(self):
        """No mode param → no finding (params default to {})."""
        from siftd.api.caveats import _search_mode_degraded_caveats
        from siftd.api.search import search_chunks
        from siftd.domain.search_types import SearchChunk

        chunk = SearchChunk(conversation_id="abc", score=0.9, text="x", chunk_type="prompt")
        op = _make_op(fn=search_chunks, render_method="search", params={})
        findings = _search_mode_degraded_caveats(op, [chunk], _make_ctx())
        assert findings == []

    def test_fts_mode_emits_hint(self):
        """mode='fts' with non-empty result → one hint finding."""
        from siftd.api.caveats import _search_mode_degraded_caveats
        from siftd.api.search import search_chunks
        from siftd.domain.search_types import SearchChunk

        chunk = SearchChunk(conversation_id="abc", score=0.8, text="y", chunk_type="fts5")
        op = _make_op(fn=search_chunks, render_method="search", params={"mode": "fts"})
        findings = _search_mode_degraded_caveats(op, [chunk], _make_ctx())
        assert len(findings) == 1

    def test_hint_fields(self):
        """Finding has correct check, severity, channel, message, fix_available."""
        from siftd.api.caveats import _search_mode_degraded_caveats
        from siftd.api.search import search_chunks
        from siftd.domain.search_types import SearchChunk

        chunk = SearchChunk(conversation_id="abc", score=0.8, text="y", chunk_type="fts5")
        op = _make_op(fn=search_chunks, render_method="search", params={"mode": "fts"})
        findings = _search_mode_degraded_caveats(op, [chunk], _make_ctx())

        f = findings[0]
        assert f.check == "search-mode-degraded"
        assert f.severity == "hint"
        assert f.channel == "both"
        assert f.message == (
            "Search running in keyword-only mode — install embeddings for semantic ranking: siftd install embed"
        )
        assert f.fix_available is False


class TestAmbiguousIdProducer:
    """Tests for the ambiguous-id producer (B9).

    The producer's applies_to predicate gates on (fn is get_conversation,
    render_method=='detail'). The producer detects when a queried ID prefix
    matches multiple conversations and warns the user to use a longer prefix.
    """

    def test_applies_to_requires_get_conversation(self):
        """Predicate is False for ops calling other functions."""
        from siftd.api.caveats import _is_detail_render

        op = _make_op(fn=lambda: {}, render_method="detail")
        assert _is_detail_render(op) is False

    def test_applies_to_requires_render_method_detail(self):
        from siftd.api.caveats import _is_detail_render
        from siftd.api.conversations import get_conversation

        op = _make_op(fn=get_conversation, render_method="list")
        assert _is_detail_render(op) is False

    def test_applies_to_satisfied(self):
        from siftd.api.caveats import _is_detail_render
        from siftd.api.conversations import get_conversation

        op = _make_op(fn=get_conversation, render_method="detail")
        assert _is_detail_render(op) is True

    def test_nonexistent_db_returns_empty(self):
        """Path doesn't exist → no finding."""
        from siftd.api.caveats import _ambiguous_id_caveats
        from siftd.api.conversations import get_conversation

        op = _make_op(
            fn=get_conversation,
            render_method="detail",
            params={"id": "01ABC"},
        )
        ctx = ProducerContext(db_path=Path("/nonexistent/path/db.sqlite"))
        assert _ambiguous_id_caveats(op, {}, ctx) == []

    def test_missing_id_param_returns_empty(self):
        """No 'id' in params → no finding."""
        from siftd.api.caveats import _ambiguous_id_caveats
        from siftd.api.conversations import get_conversation

        op = _make_op(fn=get_conversation, render_method="detail", params={})
        assert _ambiguous_id_caveats(op, {}, _make_ctx()) == []

    def test_full_ulid_no_ambiguity_check(self):
        """Full ULID (26 chars) → no DB query, no finding."""
        from siftd.api.caveats import _ambiguous_id_caveats
        from siftd.api.conversations import get_conversation

        full_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"  # 26 chars
        op = _make_op(
            fn=get_conversation,
            render_method="detail",
            params={"id": full_id},
        )
        assert _ambiguous_id_caveats(op, {}, _make_ctx()) == []

    def test_no_ambiguity_no_finding(self, monkeypatch):
        """Prefix matches exactly 1 conversation → no finding."""
        from siftd.api.caveats import _ambiguous_id_caveats
        from siftd.api.conversations import get_conversation

        class FakeConn:
            def execute(self, sql, params=None):
                return self

            def fetchone(self):
                return [1]

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )

        op = _make_op(
            fn=get_conversation,
            render_method="detail",
            params={"id": "01ARZ3"},
        )
        assert _ambiguous_id_caveats(op, {}, _make_ctx()) == []

    def test_ambiguous_id_produces_warning(self, monkeypatch, tmp_path):
        """Prefix matches multiple conversations → warning with count."""
        from siftd.api.caveats import _ambiguous_id_caveats
        from siftd.api.conversations import get_conversation

        class FakeConn:
            def execute(self, sql, params=None):
                return self

            def fetchone(self):
                return [3]

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )

        op = _make_op(
            fn=get_conversation,
            render_method="detail",
            params={"id": "01ARZ3"},
        )
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        findings = _ambiguous_id_caveats(op, {}, ctx)
        assert len(findings) == 1
        assert findings[0].check == "ambiguous-id"
        assert findings[0].severity == "warning"
        assert findings[0].fix_available is False
        assert "01ARZ3" in findings[0].message
        assert "3 conversations" in findings[0].message
        assert "showing first" in findings[0].message
        assert "longer prefix" in findings[0].message

    def test_ambiguous_id_exact_message(self, monkeypatch, tmp_path):
        """Message matches spec exactly."""
        from siftd.api.caveats import _ambiguous_id_caveats
        from siftd.api.conversations import get_conversation

        class FakeConn:
            def execute(self, sql, params=None):
                return self

            def fetchone(self):
                return [5]

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )

        op = _make_op(
            fn=get_conversation,
            render_method="detail",
            params={"id": "abc123"},
        )
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        findings = _ambiguous_id_caveats(op, {}, ctx)
        assert len(findings) == 1
        expected_msg = "ID prefix 'abc123' matched 5 conversations — showing first. Use a longer prefix to disambiguate."
        assert findings[0].message == expected_msg

    def test_db_query_uses_prefix_match(self, monkeypatch, tmp_path):
        """Verify the query uses both exact match and LIKE pattern."""
        from siftd.api.caveats import _ambiguous_id_caveats
        from siftd.api.conversations import get_conversation

        calls = []

        class FakeConn:
            def execute(self, sql, params=None):
                calls.append((sql, params))
                return self

            def fetchone(self):
                return [2]

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )

        op = _make_op(
            fn=get_conversation,
            render_method="detail",
            params={"id": "prefix"},
        )
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        findings = _ambiguous_id_caveats(op, {}, ctx)

        assert len(calls) == 1
        sql, params = calls[0]
        assert "conversations" in sql
        assert "id = ?" in sql
        assert "LIKE ?" in sql
        assert params == ("prefix", "prefix%")

    def test_two_matching_conversations(self, monkeypatch, tmp_path):
        """Count of 2 matches → finding with correct count."""
        from siftd.api.caveats import _ambiguous_id_caveats
        from siftd.api.conversations import get_conversation

        class FakeConn:
            def execute(self, sql, params=None):
                return self

            def fetchone(self):
                return [2]

            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.database.open_database", lambda *a, **kw: FakeConn(),
        )

        op = _make_op(
            fn=get_conversation,
            render_method="detail",
            params={"id": "01AB"},
        )
        db_file = tmp_path / "db.sqlite"
        db_file.touch()
        ctx = ProducerContext(db_path=db_file)
        findings = _ambiguous_id_caveats(op, {}, ctx)
        assert len(findings) == 1
        assert "2 conversations" in findings[0].message
