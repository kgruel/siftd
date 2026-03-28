"""Tests for siftd.api.sync — sync protocol utilities."""

import asyncio
import json
import sys
from types import SimpleNamespace

import asyncssh
import pytest

from siftd.api.auth import AuthError
from siftd.api.sync import (
    SyncError,
    _build_ssh_options,
    _filter_signature,
    _friendly_os_error,
    _friendly_remote_error,
    _is_http_remote,
    _parse_send_metadata,
    _pull_http,
    _pull_local,
    _pull_ssh,
    _push_http,
    _push_local,
    _push_ssh,
    _resolve_pull_since,
    _resolve_since,
    sync_pull,
    sync_push,
)
from siftd.domain.sync import (
    SYNC_HEADER,
    SYNC_MAGIC,
    SYNC_PROTOCOL_VERSION,
    SyncRemote,
    parse_sync_header,
)
from siftd.storage.sqlite import open_database


def _remote(path="/r/db", name="t", host=None, **kw):
    return SyncRemote(name=name, path=path, host=host, **kw)


def _db(tmp_path, name="l.db"):
    p = tmp_path / name
    open_database(p).close()
    return p


class TestIsHttpRemote:
    def test_local(self):
        assert not _is_http_remote(_remote())

    def test_http(self):
        assert _is_http_remote(_remote("http://x"))

    def test_https(self):
        assert _is_http_remote(_remote("https://x"))


class TestResolveSince:
    def test_explicit(self):
        assert _resolve_since("2024-01", False, _remote()) == "2024-01"

    def test_push_all(self):
        assert _resolve_since(None, True, _remote()) is None

    def test_last_push(self):
        assert _resolve_since(None, False, _remote(last_push="2024-06")) == "2024-06"

    def test_no_history(self):
        assert _resolve_since(None, False, _remote()) is None


class TestFriendlyOsError:
    def test_refused(self):
        assert "running" in _friendly_os_error("h", "Connection refused")

    def test_denied(self):
        assert "authentication" in _friendly_os_error("h", "Permission denied")

    def test_resolve(self):
        assert "resolve" in _friendly_os_error("h", "Could not resolve hostname")

    def test_name_unknown(self):
        assert "resolve" in _friendly_os_error("h", "Name or service not known")

    def test_generic(self):
        assert "SSH failed" in _friendly_os_error("h", "other")


class TestFriendlyRemoteError:
    def test_not_found(self):
        assert "not installed" in _friendly_remote_error("h", "/db", "command not found")

    def test_db_locked(self):
        assert "locked" in _friendly_remote_error("h", "/db",
                                                   json.dumps({"error_type": "database_locked", "error": "x"}))

    def test_json_err(self):
        assert "bad" in _friendly_remote_error("h", "/db", json.dumps({"error": "bad"}))

    def test_raw(self):
        assert "err" in _friendly_remote_error("h", "/db", "err\nmore")


class TestBuildSshOptions:
    def test_returns_tuple(self, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {"username": "user"})
        hostname, opts = _build_ssh_options(_remote())
        assert hostname == ""
        assert opts["username"] == "user"

    def test_parses_user_at_host(self, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        hostname, opts = _build_ssh_options(_remote(host="deploy@192.168.1.44"))
        assert hostname == "192.168.1.44"
        assert opts["username"] == "deploy"

    def test_explicit_username_wins(self, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {"username": "override"})
        hostname, opts = _build_ssh_options(_remote(host="deploy@192.168.1.44"))
        assert hostname == "192.168.1.44"
        assert opts["username"] == "override"

    def test_bare_host(self, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        hostname, opts = _build_ssh_options(_remote(host="192.168.1.44"))
        assert hostname == "192.168.1.44"
        assert "username" not in opts


class TestPushLocal:
    def test_first(self, tmp_path):
        s = tmp_path / "s.db"
        s.write_bytes(b"SQLite format 3")
        t = tmp_path / "r" / "db"
        assert not _push_local(_remote(path=str(t)), s, tmp_path / "l.db")
        assert t.exists()

    def test_merge(self, tmp_path):
        assert _push_local(
            _remote(path=str(_db(tmp_path, "r.db"))),
            _db(tmp_path, "s.db"), tmp_path / "l.db",
        )


class TestSyncPush:
    def test_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sync_push(tmp_path / "x.db", _remote())

    def test_empty(self, tmp_path):
        assert sync_push(_db(tmp_path), _remote(path=str(tmp_path / "r.db"))).conversations == 0

    def test_dry(self, tmp_path):
        assert sync_push(_db(tmp_path), _remote(path=str(tmp_path / "r.db")), dry_run=True).dry_run


class TestSyncPull:
    def test_missing(self, tmp_path):
        with pytest.raises(SyncError, match="not found"):
            sync_pull(_db(tmp_path), _remote(path=str(tmp_path / "x.db")))

    def test_empty(self, tmp_path):
        assert sync_pull(_db(tmp_path), _remote(path=str(_db(tmp_path, "r.db")))).conversations == 0


class TestParseSendMetadata:
    def test_json(self):
        assert _parse_send_metadata('{"n": 5}') == {"n": 5}

    def test_after_noise(self):
        assert _parse_send_metadata('warn\n{"ok": true}') == {"ok": True}

    def test_no_json(self):
        assert _parse_send_metadata("nope") == {}

    def test_empty(self):
        assert _parse_send_metadata("") == {}


class _HTTPStatusError(Exception):
    def __init__(self, response):
        super().__init__("http status error")
        self.response = response


class _ConnectError(Exception):
    pass


class _Resp:
    def __init__(self, *, status=200, body=None, content=b"", headers=None):
        self.status_code = status
        self._body = body or {}
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _HTTPStatusError(self)

    def json(self):
        return self._body


class _Client:
    def __init__(self, *, post_resp=None, get_resp=None, post_exc=None, get_exc=None):
        self.post_resp = post_resp
        self.get_resp = get_resp
        self.post_exc = post_exc
        self.get_exc = get_exc
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def post(self, url, *, content, headers):
        self.calls.append(("post", url, content, headers))
        if self.post_exc:
            raise self.post_exc
        return self.post_resp

    def get(self, url, *, params, headers):
        self.calls.append(("get", url, params, headers))
        if self.get_exc:
            raise self.get_exc
        return self.get_resp


class _Timeout:
    """Minimal httpx.Timeout stand-in for tests."""

    def __init__(self, connect=None, read=None, write=None, pool=None):
        self.connect = connect
        self.read = read
        self.write = write
        self.pool = pool


def _patch_httpx_module(monkeypatch, client_factory):
    fake = SimpleNamespace(
        Client=client_factory,
        Timeout=_Timeout,
        HTTPStatusError=_HTTPStatusError,
        ConnectError=_ConnectError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake)


class TestSyncPushBranches:
    def test_dry_run_non_empty_slice(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "siftd.api.slice.slice_database",
            lambda **kw: {"conversations": 2, "size_bytes": 42},
        )
        result = sync_push(_db(tmp_path), _remote(path=str(tmp_path / "r.db")), dry_run=True)
        assert result.dry_run and result.conversations == 2 and result.size_bytes == 42

    def test_http_branch_updates_last_push(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(
            "siftd.api.slice.slice_database",
            lambda **kw: {"conversations": 1, "size_bytes": 10},
        )
        monkeypatch.setattr("siftd.api.sync._push_http", lambda r, p: True)
        monkeypatch.setattr("siftd.api.sync._preflight_http", lambda *a: None)
        monkeypatch.setattr("siftd.config.update_last_push", lambda n, ts, **kw: called.append((n, ts)))
        result = sync_push(_db(tmp_path), _remote(path="http://srv"))
        assert result.remote_existed and result.last_push_updated and called

    def test_ssh_branch(self, tmp_path, monkeypatch):
        async def _fake_push_ssh(remote, slice_path, *, staged=False):
            return False

        async def _staged_preflight(*a):
            from siftd.domain.sync import SyncStatus
            return SyncStatus(capabilities=frozenset({"staged"}))

        monkeypatch.setattr(
            "siftd.api.slice.slice_database",
            lambda **kw: {"conversations": 1, "size_bytes": 7},
        )
        monkeypatch.setattr("siftd.api.sync._push_ssh", _fake_push_ssh)
        monkeypatch.setattr("siftd.api.sync._preflight_ssh", _staged_preflight)
        async def _fake_process(*a): pass
        monkeypatch.setattr("siftd.api.sync._process_remote_ssh", _fake_process)
        monkeypatch.setattr("siftd.config.update_last_sent", lambda *_, **__: None)
        result = sync_push(_db(tmp_path), _remote(path="/r.db", host="box"))
        assert result.remote_existed is False

    def test_ssh_preflight_failure_raises(self, tmp_path, monkeypatch):
        """Preflight returning None must raise, not silently fall back to blocking merge."""
        monkeypatch.setattr(
            "siftd.api.slice.slice_database",
            lambda **kw: {"conversations": 1, "size_bytes": 7},
        )
        async def _no_preflight(*a): return None
        monkeypatch.setattr("siftd.api.sync._preflight_ssh", _no_preflight)
        with pytest.raises(SyncError, match="Could not negotiate sync capabilities"):
            sync_push(_db(tmp_path), _remote(path="/r.db", host="box"))

    def test_ssh_no_staged_capability_raises(self, tmp_path, monkeypatch):
        """Remote without staged capability must raise, not fall back to blocking merge."""
        monkeypatch.setattr(
            "siftd.api.slice.slice_database",
            lambda **kw: {"conversations": 1, "size_bytes": 7},
        )
        async def _old_preflight(*a):
            from siftd.domain.sync import SyncStatus
            return SyncStatus(capabilities=frozenset())
        monkeypatch.setattr("siftd.api.sync._preflight_ssh", _old_preflight)
        with pytest.raises(SyncError, match="does not support staged receive"):
            sync_push(_db(tmp_path), _remote(path="/r.db", host="box"))

    def test_local_transport_branch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "siftd.api.slice.slice_database",
            lambda **kw: {"conversations": 1, "size_bytes": 7},
        )
        monkeypatch.setattr("siftd.api.sync._push_local", lambda *a, **k: True)
        monkeypatch.setattr("siftd.config.update_last_push", lambda *_, **__: None)
        result = sync_push(_db(tmp_path), _remote(path=str(tmp_path / "remote.db")))
        assert result.remote_existed


class TestSyncPullBranches:
    def test_http_branch_updates_last_pull(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr("siftd.api.sync._pull_http", lambda *a, **k: (2, 33))
        monkeypatch.setattr("siftd.config.update_last_pull", lambda n, ts, **kw: called.append((n, ts)))
        result = sync_pull(_db(tmp_path), _remote(path="http://srv"))
        assert result.conversations == 2 and result.last_pull_updated and called

    def test_ssh_branch_dry_run(self, tmp_path, monkeypatch):
        async def _fake_pull_ssh(*_a, **_k):
            return 2, 33

        monkeypatch.setattr("siftd.api.sync._pull_ssh", _fake_pull_ssh)
        result = sync_pull(_db(tmp_path), _remote(path="/r.db", host="box"), dry_run=True)
        assert result.dry_run and not result.last_pull_updated


class TestResolvePullSince:
    def test_explicit(self):
        assert _resolve_pull_since("2024-01", False, _remote()) == "2024-01"

    def test_pull_all(self):
        assert _resolve_pull_since(None, True, _remote(last_pull="2024-06")) is None

    def test_last_pull(self):
        assert _resolve_pull_since(None, False, _remote(last_pull="2024-06")) == "2024-06"


class TestPushHttp:
    def test_success_with_auth(self, tmp_path, monkeypatch):
        slice_path = tmp_path / "s.db"
        slice_path.write_bytes(b"db")
        client = _Client(post_resp=_Resp(body={"status": "created"}))
        _patch_httpx_module(monkeypatch, lambda timeout=None: client)
        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: {"auth": {"token": "x"}})
        monkeypatch.setattr("siftd.api.auth.acquire_token", lambda a: "tok")
        assert _push_http(_remote(path="http://srv", name="r"), slice_path) is False
        _, url, content, headers = client.calls[0]
        assert url.endswith("/api/v1/push") and content == b"db"
        assert headers["Authorization"] == "Bearer tok"

    def test_status_error(self, tmp_path, monkeypatch):
        slice_path = tmp_path / "s.db"
        slice_path.write_bytes(b"db")
        client = _Client(post_resp=_Resp(status=500, body={}))
        _patch_httpx_module(monkeypatch, lambda timeout=None: client)
        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: None)
        monkeypatch.setattr("siftd.api.auth.acquire_token", lambda a: (_ for _ in ()).throw(AuthError("no-auth")))
        with pytest.raises(SyncError, match="HTTP 500"):
            _push_http(_remote(path="http://srv"), slice_path)

    def test_connect_error(self, tmp_path, monkeypatch):
        slice_path = tmp_path / "s.db"
        slice_path.write_bytes(b"db")
        client = _Client(post_exc=_ConnectError("no route"))
        _patch_httpx_module(monkeypatch, lambda timeout=None: client)
        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: None)
        monkeypatch.setattr("siftd.api.auth.acquire_token", lambda a: (_ for _ in ()).throw(AuthError("no-auth")))
        with pytest.raises(SyncError, match="Cannot connect"):
            _push_http(_remote(path="http://srv"), slice_path)


class TestPullLocalAndHttp:
    def test_pull_local_dry_run_and_merge(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "siftd.api.slice.slice_database",
            lambda **kw: {"conversations": 2, "size_bytes": 11},
        )
        got = []
        monkeypatch.setattr("siftd.api.receive.receive_database", lambda s, d, rebuild_fts=True: got.append((s, d)))
        remote = _remote(path=str(_db(tmp_path, "remote.db")))
        local = _db(tmp_path, "local.db")
        assert _pull_local(remote, local, None, {}, True) == (2, 11)
        assert _pull_local(remote, local, None, {}, False) == (2, 11)
        assert got

    def test_pull_http_dry_run(self, tmp_path, monkeypatch):
        resp = _Resp(body={}, content=b"sqlite-bytes", headers={"X-Siftd-Conversations": "2"})
        client = _Client(get_resp=resp)
        _patch_httpx_module(monkeypatch, lambda timeout=None: client)
        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: None)
        monkeypatch.setattr("siftd.api.auth.acquire_token", lambda a: (_ for _ in ()).throw(AuthError("no-auth")))
        conv, size = _pull_http(_remote(path="http://srv"), _db(tmp_path), "2024-01", {"workspace": "proj"}, True)
        assert conv == 2 and size == len(b"sqlite-bytes")
        _, url, params, _headers = client.calls[0]
        assert url.endswith("/api/v1/pull") and params == {"since": "2024-01", "workspace": "proj"}

    def test_pull_http_full_filters(self, tmp_path, monkeypatch):
        resp = _Resp(body={}, content=b"sqlite-bytes", headers={"X-Siftd-Conversations": "1"})
        client = _Client(get_resp=resp)
        _patch_httpx_module(monkeypatch, lambda timeout=None: client)
        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: None)
        monkeypatch.setattr("siftd.api.auth.acquire_token", lambda a: (_ for _ in ()).throw(AuthError("no-auth")))
        filters = {
            "workspace": "proj",
            "tag": ["public"],
            "no_tag": ["private"],
            "owner": "alice",
        }
        _pull_http(_remote(path="http://srv"), _db(tmp_path), None, filters, True)
        _, url, params, _headers = client.calls[0]
        assert params["workspace"] == "proj"
        assert params["tag"] == ["public"]
        assert params["no_tag"] == ["private"]
        assert params["owner"] == "alice"

    def test_pull_http_merge_path(self, tmp_path, monkeypatch):
        resp = _Resp(body={}, content=b"sqlite-bytes", headers={"X-Siftd-Conversations": "1"})
        client = _Client(get_resp=resp)
        got = []
        _patch_httpx_module(monkeypatch, lambda timeout=None: client)
        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: None)
        monkeypatch.setattr("siftd.api.auth.acquire_token", lambda a: (_ for _ in ()).throw(AuthError("no-auth")))
        monkeypatch.setattr("siftd.api.receive.receive_database", lambda s, d, rebuild_fts=True: got.append((s, d)))
        conv, size = _pull_http(_remote(path="http://srv"), _db(tmp_path), None, {}, False)
        assert conv == 1 and size == len(b"sqlite-bytes") and got

    def test_pull_http_zero_and_errors(self, tmp_path, monkeypatch):
        zero_client = _Client(get_resp=_Resp(headers={"X-Siftd-Conversations": "0"}))
        _patch_httpx_module(monkeypatch, lambda timeout=None: zero_client)
        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: None)
        monkeypatch.setattr("siftd.api.auth.acquire_token", lambda a: (_ for _ in ()).throw(AuthError("no-auth")))
        assert _pull_http(_remote(path="http://srv"), _db(tmp_path), None, {}, True) == (0, 0)

        err_client = _Client(get_exc=_ConnectError("down"))
        _patch_httpx_module(monkeypatch, lambda timeout=None: err_client)
        with pytest.raises(SyncError, match="Cannot connect"):
            _pull_http(_remote(path="http://srv"), _db(tmp_path), None, {}, True)


class _Conn:
    def __init__(self, result):
        self._result = result
        self.runs = []

    async def run(self, *args, **kwargs):
        self.runs.append((args, kwargs))
        r = self._result
        if kwargs.get("encoding") is None:
            # Binary mode: convert str fields to bytes
            stdout = r.stdout.encode() if isinstance(r.stdout, str) else r.stdout
            stderr = r.stderr.encode() if isinstance(r.stderr, str) else r.stderr
            return SimpleNamespace(returncode=r.returncode, stdout=stdout, stderr=stderr)
        return r

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class TestSshErrorAndEdgePaths:
    @pytest.mark.parametrize(
        "exc",
        [
            asyncssh.DisconnectError(1, "bye"),
            asyncssh.ConnectionLost("lost"),
            asyncssh.PermissionDenied("no"),
            asyncssh.ChannelOpenError(1, "chan"),
        ],
    )
    def test_push_ssh_exception_mapping(self, tmp_path, monkeypatch, exc):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: "bad-timeout")

        def _boom(*_a, **_kw):
            raise exc

        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"db")
        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", _boom)
        with pytest.raises(SyncError):
            asyncio.run(_push_ssh(_remote(host="box"), slice_path))

    def test_push_ssh_bad_json_response(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)
        monkeypatch.setattr(
            "siftd.api.sync.asyncssh.connect",
            lambda *_a, **_kw: _Conn(SimpleNamespace(returncode=0, stdout="not-json", stderr="")),
        )
        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"db")
        with pytest.raises(SyncError, match="Unexpected response"):
            asyncio.run(_push_ssh(_remote(host="box"), slice_path))

    @pytest.mark.parametrize(
        "exc",
        [
            asyncssh.DisconnectError(1, "bye"),
            asyncssh.ConnectionLost("lost"),
            asyncssh.PermissionDenied("no"),
            asyncssh.ChannelOpenError(1, "chan"),
        ],
    )
    def test_pull_ssh_exception_mapping(self, tmp_path, monkeypatch, exc):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: "bad-timeout")

        def _boom(*_a, **_kw):
            raise exc

        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", _boom)
        with pytest.raises(SyncError):
            asyncio.run(_pull_ssh(_remote(host="box"), _db(tmp_path), None, {}, True))

    def test_pull_ssh_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        def _boom(*_a, **_kw):
            raise TimeoutError()

        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", _boom)
        with pytest.raises(SyncError, match="timed out"):
            asyncio.run(_pull_ssh(_remote(host="box"), _db(tmp_path), None, {}, True))

    def test_pull_ssh_nonzero_exit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)
        monkeypatch.setattr(
            "siftd.api.sync.asyncssh.connect",
            lambda *_a, **_kw: _Conn(SimpleNamespace(returncode=1, stdout="", stderr="bad remote")),
        )
        with pytest.raises(SyncError, match="Remote error"):
            asyncio.run(_pull_ssh(_remote(host="box"), _db(tmp_path), None, {}, True))

    def test_pull_ssh_merge_path_and_bytes_stdout(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)
        monkeypatch.setattr(
            "siftd.api.sync.asyncssh.connect",
            lambda *_a, **_kw: _Conn(SimpleNamespace(returncode=0, stdout=b"abc", stderr='{"conversations":1}')),
        )
        got = []
        monkeypatch.setattr("siftd.api.receive.receive_database", lambda s, d, rebuild_fts=True: got.append((s, d)))
        conv, size = asyncio.run(_pull_ssh(_remote(host="box"), _db(tmp_path), None, {}, False))
        assert conv == 1 and size == 3 and got

    def test_push_ssh_timeout_generic_and_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)
        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"db")

        def _timeout(*_a, **_kw):
            raise TimeoutError()

        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", _timeout)
        with pytest.raises(SyncError, match="timed out"):
            asyncio.run(_push_ssh(_remote(host="box"), slice_path))

        def _oserr(*_a, **_kw):
            raise OSError("Connection refused")

        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", _oserr)
        with pytest.raises(SyncError, match="running"):
            asyncio.run(_push_ssh(_remote(host="box"), slice_path))

        conn = _Conn(SimpleNamespace(returncode=1, stdout="", stderr="bad remote"))
        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", lambda *_a, **_kw: conn)
        with pytest.raises(SyncError, match="Remote error"):
            asyncio.run(_push_ssh(_remote(host="box"), slice_path))

    def test_push_ssh_created_response(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)
        conn = _Conn(SimpleNamespace(returncode=0, stdout='{"status":"created"}', stderr=""))
        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", lambda *_a, **_kw: conn)
        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"db")
        assert asyncio.run(_push_ssh(_remote(host="box"), slice_path)) is False

    def test_pull_ssh_since_workspace_zero_and_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        conn = _Conn(SimpleNamespace(returncode=0, stdout="", stderr='{"conversations":0}'))
        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", lambda *_a, **_kw: conn)
        assert asyncio.run(_pull_ssh(_remote(host="box"), _db(tmp_path), "2024-01", {"workspace": "proj"}, True)) == (0, 0)
        cmd = conn.runs[0][0][0]
        assert "--since" in cmd and "-w" in cmd

        conn2 = _Conn(SimpleNamespace(returncode=0, stdout="abc", stderr='{"conversations":1}'))
        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", lambda *_a, **_kw: conn2)
        conv, size = asyncio.run(_pull_ssh(_remote(host="box"), _db(tmp_path), None, {}, True))
        assert conv == 1 and size == 3

    def test_pull_ssh_generic_oserror(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        def _boom(*_a, **_kw):
            raise OSError("Connection refused")

        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", _boom)
        with pytest.raises(SyncError, match="running"):
            asyncio.run(_pull_ssh(_remote(host="box"), _db(tmp_path), None, {}, True))


class TestPullHttpAuthAndStatus:
    def test_auth_header_and_status_error(self, tmp_path, monkeypatch):
        err_client = _Client(get_resp=_Resp(status=401))
        _patch_httpx_module(monkeypatch, lambda timeout=None: err_client)
        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: {"auth": {"token": "x"}})
        monkeypatch.setattr("siftd.api.auth.acquire_token", lambda a: "tok")
        with pytest.raises(SyncError, match="HTTP 401"):
            _pull_http(_remote(path="http://srv"), _db(tmp_path), None, {}, True)


class TestPushLocalError:
    def test_merge_error(self, tmp_path, monkeypatch):
        target = _db(tmp_path, "remote.db")
        slice_db = _db(tmp_path, "slice.db")

        def _boom(**_kw):
            raise RuntimeError("merge broke")

        monkeypatch.setattr("siftd.api.merge.merge_database", _boom)
        with pytest.raises(SyncError, match="Local merge failed"):
            _push_local(_remote(path=str(target)), slice_db, _db(tmp_path, "local.db"))


class TestSyncProtocolHeader:
    def test_parse_valid_header(self):
        assert parse_sync_header(SYNC_HEADER) == SYNC_PROTOCOL_VERSION

    def test_parse_future_version(self):
        import struct
        header = SYNC_MAGIC + struct.pack(">H", 99)
        assert parse_sync_header(header) == 99

    def test_parse_no_magic(self):
        assert parse_sync_header(b"SQLite format 3\x00") is None

    def test_parse_too_short(self):
        assert parse_sync_header(b"SIFTD") is None

    def test_parse_empty(self):
        assert parse_sync_header(b"") is None

    def test_push_ssh_prepends_header(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)
        conn = _Conn(SimpleNamespace(returncode=0, stdout='{"status":"ok"}', stderr=""))
        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", lambda *_a, **_kw: conn)
        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"db-payload")
        asyncio.run(_push_ssh(_remote(host="box"), slice_path))
        sent_input = conn.runs[0][1]["input"]
        assert sent_input[:8] == SYNC_HEADER
        assert sent_input[8:] == b"db-payload"

    def test_pull_ssh_strips_header(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)
        payload = b"sqlite-data"
        stdout = SYNC_HEADER + payload
        conn = _Conn(SimpleNamespace(returncode=0, stdout=stdout, stderr='{"conversations":1}'))
        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", lambda *_a, **_kw: conn)
        contents = []
        def _capture(s, d, rebuild_fts=True):
            contents.append(s.read_bytes())
        monkeypatch.setattr("siftd.api.receive.receive_database", _capture)
        conv, size = asyncio.run(_pull_ssh(_remote(host="box"), _db(tmp_path), None, {}, False))
        assert conv == 1
        assert size == len(payload)
        assert contents[0] == payload

    def test_pull_ssh_no_header_backwards_compat(self, tmp_path, monkeypatch):
        """Old remote without protocol header still works."""
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)
        conn = _Conn(SimpleNamespace(returncode=0, stdout=b"raw-db", stderr='{"conversations":1}'))
        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", lambda *_a, **_kw: conn)
        contents = []
        def _capture(s, d, rebuild_fts=True):
            contents.append(s.read_bytes())
        monkeypatch.setattr("siftd.api.receive.receive_database", _capture)
        conv, size = asyncio.run(_pull_ssh(_remote(host="box"), _db(tmp_path), None, {}, False))
        assert conv == 1 and size == 6
        assert contents[0] == b"raw-db"

    def test_pull_ssh_future_version_error(self, tmp_path, monkeypatch):
        """Remote with a higher protocol version than we support raises SyncError."""
        import struct
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)
        future_header = SYNC_MAGIC + struct.pack(">H", SYNC_PROTOCOL_VERSION + 1)
        stdout = future_header + b"payload"
        conn = _Conn(SimpleNamespace(returncode=0, stdout=stdout, stderr='{"conversations":1}'))
        monkeypatch.setattr("siftd.api.sync.asyncssh.connect", lambda *_a, **_kw: conn)
        with pytest.raises(SyncError, match="sync protocol version"):
            asyncio.run(_pull_ssh(_remote(host="box"), _db(tmp_path), None, {}, False))


class TestFilterSignature:
    def test_empty_filters(self):
        assert _filter_signature({}) == ""

    def test_all_none_filters(self):
        assert _filter_signature({"workspace": None, "tag": None}) == ""

    def test_deterministic(self):
        f = {"workspace": "proj", "tag": ["b", "a"]}
        assert _filter_signature(f) == _filter_signature(f)

    def test_order_independent(self):
        """Lists are sorted, so order doesn't matter."""
        assert _filter_signature({"tag": ["b", "a"]}) == _filter_signature({"tag": ["a", "b"]})

    def test_different_filters_different_sig(self):
        assert _filter_signature({"workspace": "a"}) != _filter_signature({"workspace": "b"})


class TestFilterAwareCursors:
    """C3: Cursor resets when filter signature changes."""

    def test_push_same_filters_uses_cursor(self):
        sig = _filter_signature({"workspace": "proj"})
        r = _remote(last_push="2024-06", last_push_filters=sig)
        assert _resolve_since(None, False, r, sig) == "2024-06"

    def test_push_different_filters_resets_cursor(self):
        old_sig = _filter_signature({"workspace": "proj"})
        new_sig = _filter_signature({"workspace": "other"})
        r = _remote(last_push="2024-06", last_push_filters=old_sig)
        assert _resolve_since(None, False, r, new_sig) is None

    def test_push_no_stored_sig_with_new_filters_resets(self):
        """Pre-existing cursor without filter sig resets when filters are added."""
        sig = _filter_signature({"workspace": "proj"})
        r = _remote(last_push="2024-06")  # no stored filter sig
        assert _resolve_since(None, False, r, sig) is None

    def test_push_no_stored_sig_no_filters_uses_cursor(self):
        """Pre-existing cursor without filters continues working."""
        r = _remote(last_push="2024-06")
        assert _resolve_since(None, False, r, "") == "2024-06"

    def test_pull_same_filters_uses_cursor(self):
        sig = _filter_signature({"tag": ["public"]})
        r = _remote(last_pull="2024-06", last_pull_filters=sig)
        assert _resolve_pull_since(None, False, r, sig) == "2024-06"

    def test_pull_different_filters_resets_cursor(self):
        old_sig = _filter_signature({"tag": ["public"]})
        new_sig = _filter_signature({"tag": ["private"]})
        r = _remote(last_pull="2024-06", last_pull_filters=old_sig)
        assert _resolve_pull_since(None, False, r, new_sig) is None

    def test_last_sent_filters_checked(self):
        """When last_sent is used, its filter sig is checked."""
        sig = _filter_signature({"owner": "alice"})
        r = _remote(last_sent="2024-07", last_sent_filters=sig)
        assert _resolve_since(None, False, r, sig) == "2024-07"

        new_sig = _filter_signature({"owner": "bob"})
        assert _resolve_since(None, False, r, new_sig) is None


class TestStagedCursorAdvancement:
    """C1: Cursor only advances after processing is confirmed."""

    def test_staged_confirmed_advances_cursor(self, tmp_path, monkeypatch):
        """When processing succeeds, cursor is updated."""
        called = {}

        async def _fake_push_ssh(remote, slice_path, *, staged=False):
            return True

        async def _fake_preflight(*a):
            from siftd.domain.sync import SyncStatus
            return SyncStatus(capabilities=frozenset({"staged"}))

        async def _fake_process(*a):
            pass  # success

        monkeypatch.setattr(
            "siftd.api.slice.slice_database",
            lambda **kw: {"conversations": 1, "size_bytes": 10},
        )
        monkeypatch.setattr("siftd.api.sync._push_ssh", _fake_push_ssh)
        monkeypatch.setattr("siftd.api.sync._preflight_ssh", _fake_preflight)
        monkeypatch.setattr("siftd.api.sync._process_remote_ssh", _fake_process)
        monkeypatch.setattr(
            "siftd.config.update_last_sent",
            lambda n, ts, **kw: called.update(sent=ts),
        )

        result = sync_push(_db(tmp_path), _remote(path="/r.db", host="box"))
        assert result.last_push_updated
        assert "sent" in called

    def test_staged_failed_does_not_advance_cursor(self, tmp_path, monkeypatch):
        """When processing fails, cursor is NOT updated."""
        called = {}

        async def _fake_push_ssh(remote, slice_path, *, staged=False):
            return True

        async def _fake_preflight(*a):
            from siftd.domain.sync import SyncStatus
            return SyncStatus(capabilities=frozenset({"staged"}))

        async def _fake_process(*a):
            raise SyncError("remote timeout")

        monkeypatch.setattr(
            "siftd.api.slice.slice_database",
            lambda **kw: {"conversations": 1, "size_bytes": 10},
        )
        monkeypatch.setattr("siftd.api.sync._push_ssh", _fake_push_ssh)
        monkeypatch.setattr("siftd.api.sync._preflight_ssh", _fake_preflight)
        monkeypatch.setattr("siftd.api.sync._process_remote_ssh", _fake_process)
        monkeypatch.setattr(
            "siftd.config.update_last_sent",
            lambda n, ts, **kw: called.update(sent=ts),
        )

        result = sync_push(_db(tmp_path), _remote(path="/r.db", host="box"))
        assert not result.last_push_updated
        assert "sent" not in called
