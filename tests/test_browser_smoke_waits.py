"""Unit coverage for the T3 smoke's condition waits, driven by a scripted CDP wire.

The smoke itself needs a browser; these pin the wait primitives it is built on
(no fixed sleeps, named deadlines, continuous event collection) without one.
"""

import ast
import asyncio
import json
from pathlib import Path

import pytest
from browser_smoke import smoke

SECURITY_EVENT = {
    "method": "Log.entryAdded",
    "params": {"entry": {"source": "security", "text": "violation during a wait"}},
}
LOAD_EVENT = {"method": "Page.loadEventFired", "params": {"timestamp": 1.0}}


class ScriptedWire:
    """A CDP wire whose page is a function from expression to value.

    ``evaluate`` may raise ``LookupError`` to model an evaluation error reply
    (e.g. the execution context was destroyed by a navigation). Events queued
    by ``emit`` are delivered before the next reply, as a browser would.
    """

    def __init__(self, evaluate=lambda expr: True, navigate_result=None, navigate_events=()):
        self.evaluate = evaluate
        self.navigate_result = navigate_result or {"frameId": "F", "loaderId": "L"}
        self.navigate_events = list(navigate_events)
        self.expressions: list[str] = []
        self.inbox: asyncio.Queue[str] = asyncio.Queue()

    def emit(self, event) -> None:
        self.inbox.put_nowait(json.dumps(event))

    async def send(self, raw: str) -> None:
        command = json.loads(raw)
        method = command["method"]
        reply: dict = {"id": command["id"], "result": {}}
        if method == "Runtime.evaluate":
            expression = command["params"]["expression"]
            self.expressions.append(expression)
            try:
                reply["result"] = {"result": {"value": self.evaluate(expression)}}
            except LookupError as error:
                reply = {"id": command["id"], "error": {"message": str(error)}}
        elif method == "Page.navigate":
            for event in self.navigate_events:
                self.emit(event)  # may precede the reply: the race navigate() must tolerate
            reply["result"] = self.navigate_result
        self.inbox.put_nowait(json.dumps(reply))

    async def recv(self) -> str:
        return await self.inbox.get()


def run(coro):
    return asyncio.run(coro)


def test_wait_until_returns_once_the_condition_holds_and_keeps_collecting_events():
    values = iter([False, None, 0, "ready"])
    wire = ScriptedWire()

    def evaluate(expr):
        wire.emit(SECURITY_EVENT)  # a violation raised while the flow is waiting
        return next(values)

    wire.evaluate = evaluate
    cdp = smoke.CDP(wire)
    assert run(cdp.wait_until("page.ready", "page ready", timeout=5)) == "ready"
    assert len(wire.expressions) == 4
    assert cdp.security_log_entries() == ["violation during a wait"] * 4


def test_wait_until_fails_naming_the_condition_and_last_value():
    cdp = smoke.CDP(ScriptedWire(evaluate=lambda expr: 0))
    with pytest.raises(smoke.ReadinessTimeout, match=r"rail nav to stats swapped #main \(last: 0\)") as caught:
        run(cdp.wait_until("false", "rail nav to stats swapped #main", timeout=0.3))
    assert caught.value.condition == "rail nav to stats swapped #main"
    assert isinstance(caught.value, RuntimeError)  # local mode's harness-fault path still catches it


def test_wait_until_treats_an_evaluation_error_as_not_yet():
    calls = iter([LookupError("Execution context was destroyed."), True])

    def evaluate(expr):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    cdp = smoke.CDP(ScriptedWire(evaluate=evaluate))
    assert run(cdp.wait_until("x", "restored", timeout=5)) is True


def test_wait_until_reraises_an_evaluation_error_that_is_not_a_navigation():
    # A closed target or detached session is a harness fault (exit 2), not a
    # page that is slow to become ready.
    def evaluate(expr):
        raise LookupError("Target page, context or browser has been closed")

    cdp = smoke.CDP(ScriptedWire(evaluate=evaluate))
    with pytest.raises(RuntimeError, match="has been closed") as caught:
        run(cdp.wait_until("x", "restored", timeout=5))
    assert not isinstance(caught.value, smoke.ReadinessTimeout)


def test_await_outcome_returns_a_missed_outcome_instead_of_raising():
    cdp = smoke.CDP(ScriptedWire(evaluate=lambda expr: False))
    assert run(cdp.await_outcome("document.body.dataset.tone !== 'light'", timeout=0.3)) is False


def test_wait_for_swap_requires_a_settle_since_the_mark_and_no_request_in_flight():
    wire = ScriptedWire(evaluate=lambda expr: True)
    run(smoke.CDP(wire).wait_for_swap(3, "!!document.querySelector('#main .folio')", "folio mounted"))
    (expression,) = wire.expressions
    assert "(window.__settled || 0) > 3" in expression
    assert smoke.HTMX_IDLE in expression
    assert "#main .folio" in expression


def test_htmx_idle_waits_out_the_swap_and_settle_phases_not_just_the_request():
    # htmx drops .htmx-request as soon as the response is swapped in and
    # settles ~20ms later; until .htmx-settling is gone, the new content has
    # not been processed and enhance.js has not run on it.
    for phase in (".htmx-request", ".htmx-swapping", ".htmx-settling"):
        assert phase in smoke.HTMX_IDLE
    assert smoke.HTMX_IDLE.startswith("!document.querySelector(")


def test_settle_counter_is_installed_on_every_new_document():
    # wait_for_swap's mark only means "enhance.js ran" if the counter listens on
    # document (after enhance.js's body listener) for every page load.
    assert "window.__settled = 0" in smoke.LISTENER
    assert "document.addEventListener('htmx:afterSettle'" in smoke.LISTENER


def test_navigate_accepts_a_load_event_delivered_before_the_navigate_reply():
    wire = ScriptedWire(navigate_events=[LOAD_EVENT])
    cdp = smoke.CDP(wire)
    run(smoke.navigate(cdp, "http://127.0.0.1:1/", timeout=5))
    assert LOAD_EVENT in cdp.events
    assert wire.expressions == []  # no readiness expression requested


def test_navigate_ignores_an_earlier_documents_load_event():
    cdp = smoke.CDP(ScriptedWire())
    cdp.events.append(LOAD_EVENT)  # the previous page's load
    with pytest.raises(smoke.ReadinessTimeout, match="load event for http://127.0.0.1:1/"):
        run(smoke.navigate(cdp, "http://127.0.0.1:1/", timeout=0.3))


def test_navigate_then_waits_for_the_callers_readiness_condition():
    wire = ScriptedWire(evaluate=lambda expr: False, navigate_events=[LOAD_EVENT])
    with pytest.raises(smoke.ReadinessTimeout, match="shell loaded"):
        run(smoke.navigate(smoke.CDP(wire), "http://127.0.0.1:1/", "window.ready", "shell loaded", timeout=0.3))
    assert wire.expressions and set(wire.expressions) == {"window.ready"}


def test_navigate_fails_on_a_navigation_error():
    cdp = smoke.CDP(ScriptedWire(navigate_result={"errorText": "net::ERR_CONNECTION_REFUSED"}))
    with pytest.raises(RuntimeError, match="fixture navigation failed: net::ERR_CONNECTION_REFUSED"):
        run(smoke.navigate(cdp, "http://127.0.0.1:1/", timeout=0.3))


def test_session_wire_forwards_the_load_event_navigate_waits_on():
    class Session:
        def __init__(self):
            self.handlers = {}

        def on(self, method, handler):
            self.handlers[method] = handler

    session = Session()
    smoke.SessionWire(session)
    assert set(session.handlers) == set(smoke.SessionWire.FORWARDED_EVENTS)
    assert "Page.loadEventFired" in session.handlers
    assert {"Log.entryAdded", "Runtime.exceptionThrown"} <= set(session.handlers)


def test_a_flow_readiness_timeout_is_a_failed_check_and_the_verdict_still_runs(monkeypatch):
    control = json.dumps([{"blocked": "https://example.org/x.js"}])
    violation_reads = []

    def evaluate(expr):
        if expr.startswith("JSON.stringify(window.__v"):
            violation_reads.append(expr)
            return control if len(violation_reads) == 1 else "[]"
        return True

    async def stalled_flow(cdp, check, code_conv):
        raise smoke.ReadinessTimeout("rail nav to stats swapped #main", 10, False)

    monkeypatch.setattr(smoke, "flow", stalled_flow)
    results = []
    cdp = smoke.CDP(ScriptedWire(evaluate=evaluate, navigate_events=[LOAD_EVENT]))
    run(smoke.run_assertions(cdp, lambda name, ok, detail="": results.append((name, ok, detail)), "conv"))
    by_name = {name: (ok, detail) for name, ok, detail in results}
    assert by_name["positive control blocked+detected"][0] is True
    ok, detail = by_name["flow reached every readiness condition"]
    assert ok is False and "rail nav to stats swapped #main" in detail
    assert by_name["zero in-page CSP violations"][0] is True
    assert by_name["zero CDP security-log violations"][0] is True


# Ratchet: a fixed sleep used as synchronisation is the flakiness this module's
# waits replaced. Reading the wire (``drain``) and ``asyncio.sleep`` are allowed
# only as the spacing of a bounded poll, in exactly these functions. Shrink-only.
FIXED_WAIT_ALLOWLIST = {
    "drain": {"CDP.wait_for_event", "CDP.wait_until"},
    "sleep": {"wait_for_fixture", "run_local"},  # server health / CDP startup polls
}


def _calls_by_function(source: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {name: set() for name in FIXED_WAIT_ALLOWLIST}

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.ClassDef):
                inner = child.name
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                inner = f"{scope}.{child.name}" if scope else child.name
            elif isinstance(child, ast.Call):
                # Both ``cdp.drain(...)`` / ``asyncio.sleep(...)`` and a bare
                # ``sleep(...)`` from ``from asyncio import sleep``.
                func = child.func
                name = (
                    func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name)
                    else None
                )
                if name in found:
                    found[name].add(scope)
            visit(child, inner)

    visit(ast.parse(source), "")
    return found


def test_the_smoke_never_synchronises_with_a_fixed_sleep():
    source = (Path(__file__).parent / "browser_smoke" / "smoke.py").read_text()
    assert _calls_by_function(source) == FIXED_WAIT_ALLOWLIST
