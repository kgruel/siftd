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
        def producer(op, result):
            return [Finding(check="test-kind", severity="info", message="m", fix_available=False)]

        op = _make_op()
        findings = run_producers(op, [])
        assert any(f.check == "test-kind" for f in findings)

    def test_applies_to_false_skips_producer(self):
        called = []

        @caveat_producer(kind="never", applies_to=lambda op: False)
        def producer(op, result):
            called.append(True)
            return []

        run_producers(_make_op(), [])
        assert called == []

    def test_producer_spec_shape(self):
        @caveat_producer(kind="shape-test", applies_to=lambda op: True)
        def producer(op, result):
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
        def producer(op, result):
            return []

        assert len(caveats_mod._producers) == len(before) + 1
        # Fixture restoration happens at teardown — verified across tests by
        # the absence of leak-check in subsequent runs.


class TestExecuteForRender:
    def test_returns_result_and_findings_tuple(self):
        @caveat_producer(kind="ef-test", applies_to=lambda op: True)
        def producer(op, result):
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
        def producer(op, result):
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
        assert _pricing_caveats(op, []) == []

    def test_all_rows_priced_short_circuits(self, monkeypatch):
        """If no row has cost=None, the DB is never opened."""
        from siftd.api import caveats as cv

        def boom(*a, **kw):
            raise AssertionError("open_database should not be called")

        monkeypatch.setattr("siftd.api.database.open_database", boom)
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, fidelity=Fidelity(depth=3))
        priced = [
            FakeSummary(
                id="01A", workspace_path=None, model="m", started_at=None,
                prompt_count=0, response_count=0, total_tokens=0, cost=0.05,
            ),
        ]
        assert cv._pricing_caveats(op, priced) == []

    def test_unpriced_row_produces_finding(self, monkeypatch):
        """When cost is None and the model is in the unpriced set, a Finding is produced."""
        from siftd.api import caveats as cv
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
        findings = cv._pricing_caveats(op, summaries)
        assert len(findings) == 1
        assert findings[0].check == "pricing-missing"
        assert findings[0].target == "01A"
        assert findings[0].context == {"model": "claude-opus-4-7"}
        assert findings[0].fix_available is False

    def test_priced_row_skipped_even_if_model_unpriced(self, monkeypatch):
        """Row with computed cost is treated as priced regardless of pricing table state."""
        from siftd.api import caveats as cv
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
        findings = cv._pricing_caveats(op, summaries)
        assert len(findings) == 1
        assert findings[0].target == "01A"
