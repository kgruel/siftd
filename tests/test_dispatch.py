"""Tests for siftd.api.dispatch — Operation IR and execution."""

from dataclasses import dataclass
from pathlib import Path

from siftd.api.dispatch import Operation, dispatch, execute, render


@dataclass
class _Fidelity:
    visible: frozenset = frozenset({"text"})


def _make_op(fn=None, render_method="raw", params=None, *, path="/test", method="GET"):
    return Operation(
        path=path,
        method=method,
        fn=fn or (lambda: "result"),
        params=params or {},
        render_method=render_method,
        fidelity=_Fidelity(),
        db=Path("/test.db"),
    )


class TestExecute:
    def test_calls_fn(self):
        assert execute(_make_op(fn=lambda: 42)) == 42

    def test_strips_local_excludes(self):
        """execute() drops keys the spec marks as local-excluded.

        Uses ``/api/v1/search`` because its OpSpec lists ``action`` in
        ``local_excludes`` — a CLI routing key the local fn doesn't accept.
        """
        op = _make_op(
            path="/api/v1/search",
            fn=lambda x=1: x,
            params={"x": 5, "action": "ignored"},
        )
        assert execute(op) == 5


class TestRender:
    def test_raw_passthrough(self):
        assert render("data", _make_op(render_method="raw"), fmt=None) == "data"

    def test_missing_renderer(self):
        assert render("data", _make_op(render_method="nosuch"), fmt=object()) == "data"

    def test_calls_renderer(self):
        class Fmt:
            def render_list(self, result, fidelity):
                return f"rendered:{result}"

        assert render("x", _make_op(render_method="list"), fmt=Fmt()) == "rendered:x"


class TestDispatch:
    def test_execute_and_render(self):
        class Fmt:
            def render_detail(self, result, fidelity):
                return f"detail:{result}"

        op = _make_op(fn=lambda: "data", render_method="detail")
        assert dispatch(op, fmt=Fmt()) == "detail:data"
