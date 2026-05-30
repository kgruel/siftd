"""I1 — push failures surface a structured, actionable error (client side).

The serve push route returns {error, error_type} for client-fixable failures;
_friendly_http_push_error turns that into a useful message instead of the bare
"Push failed: HTTP 500" that previously hid the cause (a version-mismatched
fleet member failed every push unactionably).
"""

from __future__ import annotations

from siftd.api.sync import _friendly_http_push_error


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


def test_locked_suggests_retry():
    msg = _friendly_http_push_error(_Resp(503, {"error": "db is locked", "error_type": "database_locked"}))
    assert "locked" in msg.lower()
    assert "retry" in msg.lower()


def test_schema_mismatch_prompts_upgrade_and_keeps_detail():
    msg = _friendly_http_push_error(
        _Resp(409, {"error": "target is v8, source is v7", "error_type": "schema_mismatch"})
    )
    assert "upgrade" in msg.lower()
    assert "v8" in msg and "v7" in msg


def test_invalid_source_is_explained():
    msg = _friendly_http_push_error(
        _Resp(400, {"error": "Not a valid SQLite database", "error_type": "invalid_source"})
    )
    assert "invalid" in msg.lower()
    assert "SQLite" in msg


def test_generic_error_body_is_surfaced():
    msg = _friendly_http_push_error(_Resp(400, {"error": "something specific"}))
    assert "something specific" in msg


def test_no_json_body_falls_back_to_status():
    assert _friendly_http_push_error(_Resp(500, None)) == "Push failed: HTTP 500"
