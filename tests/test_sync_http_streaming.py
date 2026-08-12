"""Tests for Y3: HTTP streaming for sync push and pull.

Covers:
- Client push: streams file in chunks, never calls read_bytes()
- Server push: consumes chunked request body, not buffered
- Server pull: mkdtemp + File + BackgroundTask, not TemporaryDirectory
- Temp-file lifetime: slice file outlives handler return; cleanup runs after streaming
- Dry-run query param: ?dry_run=1 returns count estimate, skips slice_database
- Zero-conversation pull: returns empty headers without creating a temp file
- BackgroundTask cleanup resilience: logs warning and completes on cleanup failure
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from litestar.testing import TestClient

from siftd.api.sync import SYNC_HTTP_CHUNK_SIZE, SyncError, _pull_http, _push_slice_http
from siftd.domain.sync import SyncRemote
from siftd.serve.app import create_app


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _remote(path="http://srv", name="t"):
    return SyncRemote(name=name, path=path, host=None)


def _make_team_db(path, *, conversations=None):
    from siftd.storage.sqlite import (
        create_database,
        get_or_create_harness,
        get_or_create_model,
        get_or_create_provider,
        get_or_create_workspace,
        insert_conversation,
        insert_prompt,
        insert_prompt_content,
        insert_response,
        insert_response_content,
    )

    conn = create_database(path)
    h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
    w = get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    m = get_or_create_model(conn, "gpt-4")
    p = get_or_create_provider(conn, "openai")
    for conv in conversations or []:
        started = conv.get("started_at", "2024-01-15T10:00:00Z")
        cid = insert_conversation(
            conn, external_id=conv["external_id"], harness_id=h,
            workspace_id=w, started_at=started,
        )
        pid = insert_prompt(conn, cid, f"p-{conv['external_id']}", started)
        insert_prompt_content(conn, pid, 0, "text", '{"text": "hello"}')
        rid = insert_response(
            conn, cid, pid, m, p, f"r-{conv['external_id']}", started,
            input_tokens=10, output_tokens=5,
        )
        insert_response_content(conn, rid, 0, "text", '{"text": "hi"}')
    conn.commit()
    conn.close()
    return path


def _make_slice_bytes(tmp_path, *, external_id="c1"):
    from siftd.api.slice import slice_database

    source = _make_team_db(
        tmp_path / "source.db",
        conversations=[{"external_id": external_id}],
    )
    slice_path = tmp_path / "slice.db"
    slice_database(source, slice_path, rebuild_fts=False)
    return slice_path.read_bytes()


# ---------------------------------------------------------------------------
# Fake httpx helpers for client-side unit tests
# ---------------------------------------------------------------------------


class _FakeHTTPStatusError(Exception):
    def __init__(self, response):
        super().__init__("http status error")
        self.response = response


class _FakeConnectError(Exception):
    pass


class _StreamResp:
    """Fake streaming HTTP response for client-side tests."""

    def __init__(self, *, status=200, headers=None, content=b""):
        self.status_code = status
        self.headers = headers or {}
        self._content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _FakeHTTPStatusError(self)

    def iter_bytes(self, *, chunk_size=None):
        data = self._content
        if not data:
            return
        size = chunk_size or len(data)
        for i in range(0, len(data), size):
            yield data[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _PostResp:
    def __init__(self, *, status=200, body=None):
        self.status_code = status
        self._body = body or {"status": "ok"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _FakeHTTPStatusError(self)

    def json(self):
        return self._body


def _patch_fake_httpx(monkeypatch, *, stream_resp=None, stream_exc=None,
                       post_resp=None, post_exc=None):
    """Install a fake httpx module supporting streaming and return the client tracker."""
    chunks_per_call: list[list[bytes]] = []
    post_calls: list[dict] = []
    stream_calls: list[dict] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, url, *, content=None, headers=None):
            chunks: list[bytes] = []
            if hasattr(content, "__iter__") and not isinstance(content, (bytes, bytearray)):
                for c in content:
                    chunks.append(c)
                content_bytes = b"".join(chunks)
            else:
                content_bytes = content or b""
            chunks_per_call.append(chunks)
            post_calls.append({"url": url, "content": content_bytes, "headers": headers})
            if post_exc:
                raise post_exc
            return post_resp

        @contextmanager
        def stream(self, method, url, *, params=None, headers=None):
            stream_calls.append({"method": method, "url": url, "params": params or {}})
            if stream_exc:
                raise stream_exc
            yield stream_resp

    fake = SimpleNamespace(
        Client=lambda **kw: FakeClient(),
        Timeout=lambda **kw: None,
        HTTPStatusError=_FakeHTTPStatusError,
        ConnectError=_FakeConnectError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake)

    tracker = SimpleNamespace(
        chunks_per_call=chunks_per_call,
        post_calls=post_calls,
        stream_calls=stream_calls,
    )
    return tracker


def _setup_client_auth(monkeypatch):
    """Patch config/auth so client HTTP functions work without a real server."""
    from siftd.api.auth import AuthError

    monkeypatch.setattr("siftd.config_sync.get_sync_remote", lambda n: {})
    monkeypatch.setattr("siftd.config_sync.get_sync_timeouts", lambda n, k: (5, 30))
    monkeypatch.setattr(
        "siftd.api.auth.acquire_token",
        lambda *a: (_ for _ in ()).throw(AuthError("no-auth")),
    )


# ---------------------------------------------------------------------------
# Client push streaming
# ---------------------------------------------------------------------------


class _CapturingClient:
    """Minimal fake httpx client that records post() chunks and returns a fixed response."""

    def __init__(self, resp):
        self._resp = resp
        self.chunks_per_call: list[list[bytes]] = []
        self.post_calls: list[dict] = []

    def post(self, url, *, content=None, headers=None):
        chunks: list[bytes] = []
        if hasattr(content, "__iter__") and not isinstance(content, (bytes, bytearray)):
            for c in content:
                chunks.append(c)
        else:
            chunks = [content] if content else []
        self.chunks_per_call.append(chunks)
        self.post_calls.append({"url": url, "headers": headers})
        return self._resp


class TestPushHttpStreaming:
    """_push_slice_http must stream the file rather than reading it all into memory."""

    def test_yields_multiple_chunks_for_large_file(self, tmp_path):
        """A >2 MiB file must produce at least 3 distinct chunk yields."""
        data_size = SYNC_HTTP_CHUNK_SIZE * 2 + 100
        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"S" * data_size)

        client = _CapturingClient(_PostResp(body={"status": "ok"}))
        _push_slice_http(client, "http://srv/api/v1/push", {}, slice_path)

        assert client.post_calls, "post() was not called"
        chunks = client.chunks_per_call[0]
        assert len(chunks) >= 3, f"Expected ≥3 chunks for {data_size} bytes, got {len(chunks)}"
        assert sum(len(c) for c in chunks) == data_size

    def test_content_length_header_is_set(self, tmp_path):
        """Content-Length must be the actual file size so proxies can handle chunked bodies."""
        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"X" * 500)

        client = _CapturingClient(_PostResp(body={"status": "ok"}))
        _push_slice_http(client, "http://srv/api/v1/push", {}, slice_path)

        headers = client.post_calls[0]["headers"]
        assert headers["Content-Length"] == "500"
        assert headers["Content-Type"] == "application/octet-stream"

    def test_does_not_call_read_bytes_on_slice(self, tmp_path):
        """Streaming push must not buffer the entire slice with Path.read_bytes()."""
        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"A" * (SYNC_HTTP_CHUNK_SIZE + 100))

        read_bytes_called = []
        original = Path.read_bytes

        def spy_read_bytes(self_path):
            if self_path == slice_path:
                read_bytes_called.append(self_path)
            return original(self_path)

        client = _CapturingClient(_PostResp(body={"status": "ok"}))
        with patch.object(Path, "read_bytes", spy_read_bytes):
            _push_slice_http(client, "http://srv/api/v1/push", {}, slice_path)

        assert not read_bytes_called, "read_bytes() was called on the slice path"


# ---------------------------------------------------------------------------
# Server push streaming
# ---------------------------------------------------------------------------


class TestServerPushStreaming:
    """Push route must consume the request body via stream(), not buffer with request.body()."""

    def test_streamed_post_merges_successfully(self, tmp_path):
        """Valid DB pushed via streaming POST is merged into the team DB."""
        slice_bytes = _make_slice_bytes(tmp_path)
        team_db = tmp_path / "team.db"
        app = create_app(db_path=team_db, auth_config=None)

        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/push",
                content=slice_bytes,
                headers={"Content-Type": "application/octet-stream"},
            )

        assert resp.status_code == 201
        assert resp.json()["conversations"] >= 1

    def test_empty_body_rejected_with_400(self, tmp_path):
        """Bodies smaller than 16 bytes must be rejected."""
        team_db = tmp_path / "team.db"
        app = create_app(db_path=team_db, auth_config=None)

        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/push",
                content=b"tiny",
                headers={"Content-Type": "application/octet-stream"},
            )

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Server pull round-trip (streaming)
# ---------------------------------------------------------------------------


class TestServerPullRoundTrip:
    """Pull route must stream the slice via File response."""

    def test_pull_returns_valid_sqlite(self, tmp_path):
        """GET /api/v1/pull returns a SQLite file with the expected conversation count."""
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )
        app = create_app(db_path=team_db, auth_config=None)

        with TestClient(app) as client:
            resp = client.get("/api/v1/pull")

        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "application/octet-stream"
        assert int(resp.headers["X-Siftd-Conversations"]) >= 1
        assert resp.content[:16].startswith(b"SQLite format 3")

    def test_pull_x_siftd_size_matches_body(self, tmp_path):
        """X-Siftd-Size header must equal the actual response body size."""
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )
        app = create_app(db_path=team_db, auth_config=None)

        with TestClient(app) as client:
            resp = client.get("/api/v1/pull")

        assert resp.status_code == 200
        expected_size = int(resp.headers["X-Siftd-Size"])
        assert expected_size == len(resp.content)


# ---------------------------------------------------------------------------
# Server pull temp-file lifetime (critical acceptance gate)
# ---------------------------------------------------------------------------


class TestServerPullTempFileLifetime:
    """The temp dir must outlive the handler so File can stream it; cleanup must follow."""

    def test_body_readable_after_handler_returns_and_dir_cleaned_up(self, tmp_path, monkeypatch):
        """mkdtemp + BackgroundTask: file is readable after handler return; dir gone after stream."""
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )
        app = create_app(db_path=team_db, auth_config=None)

        recorded_dirs: list[str] = []
        original_mkdtemp = tempfile.mkdtemp

        def tracking_mkdtemp(*args, **kwargs):
            d = original_mkdtemp(*args, **kwargs)
            if "siftd-serve-pull" in kwargs.get("prefix", ""):
                recorded_dirs.append(d)
            return d

        monkeypatch.setattr(tempfile, "mkdtemp", tracking_mkdtemp)

        with TestClient(app) as client:
            resp = client.get("/api/v1/pull")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        # If the temp dir was cleaned up at handler return (naive TemporaryDirectory),
        # File streaming would fail and the body would not be valid SQLite.
        assert resp.content[:16].startswith(b"SQLite format 3"), (
            "Response body not valid SQLite — temp dir was likely cleaned up too early"
        )

        # BackgroundTask must have run by the time TestClient.get() returns
        assert recorded_dirs, "mkdtemp was not called with siftd-serve-pull prefix"
        for d in recorded_dirs:
            assert not Path(d).exists(), (
                f"Temp dir {d} still exists after response — BackgroundTask did not clean up"
            )


# ---------------------------------------------------------------------------
# Dry-run query param
# ---------------------------------------------------------------------------


class TestServerPullDryRun:
    """?dry_run=1 must return count + size estimate without calling slice_database()."""

    def test_dry_run_returns_empty_body_with_headers(self, tmp_path):
        """Dry-run returns empty body, X-Siftd-Size: 0, and conversation count."""
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )
        app = create_app(db_path=team_db, auth_config=None)

        with TestClient(app) as client:
            resp = client.get("/api/v1/pull", params={"dry_run": "1"})

        assert resp.status_code == 200
        assert resp.content == b"", f"Expected empty body, got {len(resp.content)} bytes"
        assert resp.headers["X-Siftd-Size"] == "0", (
            "X-Siftd-Size must be 0 for dry-run (slice was not created)"
        )
        assert int(resp.headers["X-Siftd-Conversations"]) >= 1
        # X-Siftd-Estimated-Size carries the approximation (may be 0 if no estimator)
        assert "X-Siftd-Estimated-Size" in resp.headers
        assert int(resp.headers["X-Siftd-Estimated-Size"]) >= 0

    def test_dry_run_does_not_call_slice_database(self, tmp_path, monkeypatch):
        """slice_database() must not be called for dry-run requests."""
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )
        app = create_app(db_path=team_db, auth_config=None)

        slice_called = []

        original_slice = None

        def spy_slice(**kw):
            slice_called.append(kw)
            return original_slice(**kw)

        import siftd.api.slice as _slice_mod

        original_slice = _slice_mod.slice_database
        monkeypatch.setattr(_slice_mod, "slice_database", spy_slice)

        with TestClient(app) as client:
            resp = client.get("/api/v1/pull", params={"dry_run": "1"})

        assert resp.status_code == 200
        assert not slice_called, "slice_database() was called during dry-run"

    def test_dry_run_estimated_size_is_nonzero_for_nonempty_db(self, tmp_path):
        """X-Siftd-Estimated-Size should be > 0 for a non-empty database."""
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )
        app = create_app(db_path=team_db, auth_config=None)

        with TestClient(app) as client:
            resp = client.get("/api/v1/pull", params={"dry_run": "1"})

        assert resp.status_code == 200
        estimated = int(resp.headers.get("X-Siftd-Estimated-Size", 0))
        # The estimator uses page_count * page_size; for any non-empty DB this is > 0.
        assert estimated > 0


# ---------------------------------------------------------------------------
# Zero-conversation pull
# ---------------------------------------------------------------------------


class TestServerPullZeroConversations:
    """Empty DB returns X-Siftd-Conversations: 0 without creating a temp file."""

    def test_empty_db_returns_zero_header(self, tmp_path):
        from siftd.storage.sqlite import create_database

        team_db = tmp_path / "team.db"
        create_database(team_db)
        app = create_app(db_path=team_db, auth_config=None)

        with TestClient(app) as client:
            resp = client.get("/api/v1/pull")

        assert resp.status_code == 200
        assert int(resp.headers.get("X-Siftd-Conversations", -1)) == 0

    def test_empty_db_does_not_leave_temp_dirs(self, tmp_path, monkeypatch):
        """Zero-conversation pull must not leave orphaned temp directories."""
        from siftd.storage.sqlite import create_database

        team_db = tmp_path / "team.db"
        create_database(team_db)
        app = create_app(db_path=team_db, auth_config=None)

        created_dirs: list[str] = []
        original_mkdtemp = tempfile.mkdtemp

        def tracking_mkdtemp(*args, **kwargs):
            d = original_mkdtemp(*args, **kwargs)
            if "siftd-serve-pull" in kwargs.get("prefix", ""):
                created_dirs.append(d)
            return d

        monkeypatch.setattr(tempfile, "mkdtemp", tracking_mkdtemp)

        with TestClient(app) as client:
            client.get("/api/v1/pull")

        for d in created_dirs:
            assert not Path(d).exists(), f"Orphaned temp dir: {d}"


# ---------------------------------------------------------------------------
# BackgroundTask cleanup resilience
# ---------------------------------------------------------------------------


class TestBackgroundTaskCleanupResilience:
    """Cleanup failure in BackgroundTask must log a warning and not break the response."""

    def test_response_succeeds_when_cleanup_raises(self, tmp_path):
        import logging

        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )
        app = create_app(db_path=team_db, auth_config=None)

        # Capture logs from the routes logger manually — BackgroundTask runs in
        # the ASGI context and may not propagate to pytest's caplog fixture.
        warning_records: list[logging.LogRecord] = []

        class Catcher(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                warning_records.append(record)

        handler = Catcher(level=logging.WARNING)
        routes_logger = logging.getLogger("siftd.serve.routes")
        routes_logger.addHandler(handler)

        import siftd.serve.routes as routes_mod

        try:
            with patch.object(routes_mod.shutil, "rmtree", side_effect=OSError("disk full")):
                with TestClient(app) as client:
                    resp = client.get("/api/v1/pull")
        finally:
            routes_logger.removeHandler(handler)

        assert resp.status_code == 200
        assert resp.content[:16].startswith(b"SQLite format 3")
        assert any("Failed to clean up" in r.getMessage() for r in warning_records), (
            "Expected a WARNING log about cleanup failure"
        )


# ---------------------------------------------------------------------------
# Date-param parsing at the HTTP boundary (#32)
# ---------------------------------------------------------------------------


@contextmanager
def _team_client(tmp_path=None):
    """A TestClient over a one-conversation team DB."""
    import tempfile as _tf

    with _tf.TemporaryDirectory() as tmp:
        base = tmp_path if tmp_path is not None else Path(tmp)
        team_db = _make_team_db(base / "team.db", conversations=[{"external_id": "c1"}])
        with TestClient(create_app(db_path=team_db, auth_config=None)) as c:
            yield c


class TestDateParamsAreParsedAtTheBoundary:
    """serve is an input context and had no equivalent of argparse's `date_arg`.

    Every `since`/`before` went into `started_at >= ?` exactly as typed. The
    filter is a *string* comparison, so an unparseable value neither matched
    nor complained — it silently degraded the request instead of rejecting it.
    Invisible from siftd's own clients, which always send a parsed value.
    """

    ROUTES = (
        "/api/v1/pull",
        "/api/v1/conversations",
        "/api/v1/tags",
        "/api/v1/export",
    )

    @pytest.fixture
    def client(self, tmp_path):
        with _team_client(tmp_path) as c:
            yield c

    @pytest.mark.parametrize("route", ROUTES)
    @pytest.mark.parametrize("param", ["since", "before"])
    def test_an_unparseable_value_is_rejected(self, client, route, param):
        resp = client.get(route, params={param: "lastweek"})
        assert resp.status_code == 400
        assert "lastweek" in resp.json()["error"]

    def test_a_partial_iso_date_is_rejected_not_widened(self, client):
        """`2024-01` is the shape #21 found on the wire. `parse_date` rejects
        it; raw, it matched every row in January by prefix.

        One route, not the sweep: the per-route parametrize above already
        proves each handler is wired, and which *values* `parse_date` rejects
        is its own tests' business."""
        resp = client.get("/api/v1/pull", params={"since": "2024-01"})
        assert resp.status_code == 400

    @pytest.mark.parametrize("route", ROUTES)
    def test_the_vocabulary_the_cli_accepts_works_here_too(self, client, route):
        """Both input contexts run `parse_date`, so relative forms resolve
        rather than being compared as the literal string '7d'."""
        assert client.get(route, params={"since": "7d"}).status_code == 200

    def test_an_already_parsed_value_still_round_trips(self, client):
        """siftd's own clients send `date_arg` output; `parse_date` accepts it."""
        resp = client.get("/api/v1/pull", params={"since": "2024-01-15T09:30:12"})
        assert resp.status_code == 200

    def test_the_htmx_ui_is_the_third_input_context(self):
        """`/query` filters too, and it is not a JSON route.

        serve has two boundaries, not one: `routes.py` answers the JSON API
        and `html_routes.py` answers the htmx UI, and `/query` passes its date
        facets into `search_view` and a browse Operation without ever building
        one of `routes.py`'s handlers. It answers in fragments, so it renders
        the rejection as HTML rather than letting the app-level JSON handler
        put an error envelope inside a pane.

        `ui_shell`/`ui_find`/`ui_meta` take the same params and are *not*
        checked here: they only echo them back as URL-as-state so the filter
        strip prefills what the user typed. Parsing there would redisplay
        `7d` as a date.
        """
        htmx = {"HX-Request": "true"}  # else _shell_redirect 303s to the shell
        with _team_client() as client:
            resp = client.get("/query", params={"since": "lastweek"}, headers=htmx)
            assert resp.status_code == 400
            assert "text/html" in resp.headers["content-type"]
            assert "lastweek" in resp.text

            ok = client.get("/query", params={"since": "7d"}, headers=htmx)
            assert ok.status_code == 200
