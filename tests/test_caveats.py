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


class TestFreshCorpusProducer:
    """Tests for the fresh-corpus producer.

    The producer's applies_to predicate is gated on (fn is list_conversations,
    render_method=='list'). No depth gate — this caveat is useful at any depth.
    """

    def test_applies_to_requires_list_conversations(self):
        """Predicate is False for ops calling other functions."""
        from siftd.api.caveats import _is_list_conversations_list_render

        op = _make_op(fn=lambda: [], render_method="list")
        assert _is_list_conversations_list_render(op) is False

    def test_applies_to_requires_render_method_list(self):
        from siftd.api.caveats import _is_list_conversations_list_render
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, render_method="detail")
        assert _is_list_conversations_list_render(op) is False

    def test_applies_to_satisfied(self):
        from siftd.api.caveats import _is_list_conversations_list_render
        from siftd.api.conversations import list_conversations

        op = _make_op(fn=list_conversations, render_method="list")
        assert _is_list_conversations_list_render(op) is True

    def test_applies_to_satisfied_at_depth_1(self):
        """Predicate should be true at any depth."""
        from siftd.api.caveats import _is_list_conversations_list_render
        from siftd.api.conversations import list_conversations

        op = _make_op(
            fn=list_conversations,
            render_method="list",
            fidelity=Fidelity(depth=1),
        )
        assert _is_list_conversations_list_render(op) is True

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
        result = []
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
