# `siftd serve` Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an HTTP team sync server (`siftd serve`) that exposes push/pull/search/query over the network with OAuth authentication, plus the client-side HTTP transport to use it.

**Architecture:** Litestar app wrapping existing SQLite primitives (receive, slice, search, query). Two auth modes: OIDC JWT and RFC 7662 introspection. Client detects HTTP remotes by URL prefix. Push log table for attribution. Optional `[serve]` extra.

**Tech Stack:** Litestar, uvicorn, PyJWT (OIDC), httpx (introspection + client transport)

**Design doc:** `docs/plans/2026-02-26-siftd-serve-design.md`

---

### Task 1: Package scaffolding & import gating

Set up the `[serve]` optional extra, pytest marker, and import guard so the rest of the tasks have a foundation.

**Files:**
- Modify: `pyproject.toml:28-59`
- Create: `src/siftd/serve/__init__.py`
- Create: `tests/test_serve.py`

**Step 1: Add `serve` extra to pyproject.toml**

Add after the `embed` extra (line 35):

```toml
serve = [
    "litestar[standard]",
    "uvicorn",
    "httpx",
    "PyJWT[crypto]",
]
```

Add pytest marker after `embeddings` marker (line 59):

```python
"serve: tests that require serve dependencies",
```

**Step 2: Create import guard in `serve/__init__.py`**

```python
"""HTTP team sync server.

Requires the ``[serve]`` optional extra::

    pip install siftd[serve]
"""

from __future__ import annotations


def require_serve(feature: str = "siftd serve") -> None:
    """Raise if serve dependencies are not installed."""
    try:
        import litestar  # noqa: F401
    except ImportError:
        raise ImportError(
            f"{feature} requires the [serve] extra. "
            "Install with: pip install siftd[serve]"
        ) from None
```

**Step 3: Create test file skeleton**

```python
"""Tests for siftd serve — HTTP team sync server."""

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve
```

**Step 4: Run tests to verify marker works**

Run: `./dev test`
Expected: PASS (new test file collected but empty, marker registered)

**Step 5: Install serve deps in local venv**

Run: `uv sync --extra dev --extra serve`

**Step 6: Commit**

```bash
git add pyproject.toml src/siftd/serve/__init__.py tests/test_serve.py
git commit -m "Add [serve] optional extra and import guard"
```

---

### Task 2: Client config — auth section & token acquisition

Extend remote config to carry auth settings. Add token acquisition helper.

**Files:**
- Modify: `src/siftd/config.py:400-414` (get_sync_remotes)
- Modify: `src/siftd/config.py:428-464` (set_sync_remote)
- Create: `src/siftd/api/auth.py`
- Modify: `tests/test_sync.py` (add auth config tests)

**Step 1: Write failing test — auth config round-trips through remote CRUD**

In `tests/test_sync.py`, add to `TestConfigRemotes`:

```python
def test_remote_auth_config_roundtrip(self, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    set_sync_remote("team", None, "https://siftd.example.com")
    set_remote_auth("team", {"token_command": "gh auth token"})
    remote = get_sync_remote("team")
    assert remote["auth"] == {"token_command": "gh auth token"}

def test_remote_without_auth(self, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    set_sync_remote("local", None, "/tmp/team.db")
    remote = get_sync_remote("local")
    assert remote["auth"] is None
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_sync.py::TestConfigRemotes::test_remote_auth_config_roundtrip -v`
Expected: FAIL (no `set_remote_auth` function)

**Step 3: Implement config changes**

In `config.py`, extend `get_sync_remotes()` (~line 407) to include auth:

```python
remotes.append({
    "name": name,
    "host": cfg.get("host"),
    "path": str(cfg.get("path", "")),
    "last_push": cfg.get("last_push"),
    "last_pull": cfg.get("last_pull"),
    "auth": dict(cfg["auth"]) if "auth" in cfg and isinstance(cfg.get("auth"), dict) else None,
})
```

Add `set_remote_auth()`:

```python
def set_remote_auth(name: str, auth: dict) -> None:
    """Set auth config for a sync remote."""
    cfg_path = config_file()
    doc = tomlkit.parse(cfg_path.read_text())
    remote_tbl = cast(Container, doc["sync"]["remotes"][name])
    remote_tbl["auth"] = auth
    cfg_path.write_text(tomlkit.dumps(doc))
```

Update imports in `test_sync.py` to include `set_remote_auth`.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sync.py::TestConfigRemotes -v`
Expected: PASS

**Step 5: Write failing test — token acquisition**

Create `tests/test_auth.py`:

```python
"""Tests for token acquisition from remote config."""

from siftd.api.auth import acquire_token, AuthError


class TestAcquireToken:
    def test_token_command(self, monkeypatch):
        auth = {"token_command": "echo test-token-123"}
        token = acquire_token(auth)
        assert token == "test-token-123"

    def test_token_env_var(self, monkeypatch):
        monkeypatch.setenv("SIFTD_TOKEN", "env-token-456")
        auth = {"token": "env:SIFTD_TOKEN"}
        token = acquire_token(auth)
        assert token == "env-token-456"

    def test_token_file(self, tmp_path):
        token_file = tmp_path / "token.txt"
        token_file.write_text("file-token-789\n")
        auth = {"token": f"file:{token_file}"}
        token = acquire_token(auth)
        assert token == "file-token-789"

    def test_no_auth_config_raises(self):
        with pytest.raises(AuthError, match="no auth configured"):
            acquire_token({})

    def test_none_auth_raises(self):
        with pytest.raises(AuthError, match="no auth configured"):
            acquire_token(None)
```

**Step 6: Run test to verify it fails**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL (module doesn't exist)

**Step 7: Implement `api/auth.py`**

```python
"""Authentication helpers for sync remotes and serve."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class AuthError(Exception):
    """Raised when token acquisition fails."""


def acquire_token(auth: dict | None) -> str:
    """Acquire a bearer token from auth config.

    Resolution order: token_command > token (env:/file:/literal).

    Raises:
        AuthError: If no auth is configured or token acquisition fails.
    """
    if not auth:
        raise AuthError("no auth configured for remote")

    if cmd := auth.get("token_command"):
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                raise AuthError(f"token command failed: {result.stderr.strip()}")
            return result.stdout.strip()
        except subprocess.TimeoutExpired as e:
            raise AuthError(f"token command timed out: {cmd}") from e

    if token_ref := auth.get("token"):
        if token_ref.startswith("env:"):
            env_var = token_ref[4:]
            value = os.environ.get(env_var)
            if not value:
                raise AuthError(f"environment variable not set: {env_var}")
            return value
        if token_ref.startswith("file:"):
            path = Path(token_ref[5:]).expanduser()
            if not path.exists():
                raise AuthError(f"token file not found: {path}")
            return path.read_text().strip()
        return token_ref  # literal

    raise AuthError("no auth configured for remote")
```

**Step 8: Run tests**

Run: `pytest tests/test_auth.py -v`
Expected: PASS

**Step 9: Commit**

```bash
git add src/siftd/config.py src/siftd/api/auth.py tests/test_sync.py tests/test_auth.py
git commit -m "Add remote auth config and token acquisition"
```

---

### Task 3: HTTP transport — client-side push & pull

Add `_push_http()` and `_pull_http()` to `sync.py`, update transport branching.

**Files:**
- Modify: `src/siftd/api/sync.py:96-99` (push branching)
- Modify: `src/siftd/api/sync.py:294-301` (pull branching)
- Modify: `tests/test_sync.py` (HTTP transport tests)

**Step 1: Write failing test — HTTP push**

In `tests/test_sync.py`, add:

```python
class TestHTTPPush:
    def test_push_http_posts_slice(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        (tmp_path / "config").mkdir()
        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-1"}],
        )
        set_sync_remote("team", None, "https://siftd.example.com")
        set_remote_auth("team", {"token": "test-token"})

        import httpx
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "created", "conversations": 1}
        mock_response.raise_for_status = MagicMock()

        with patch("siftd.api.sync.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.post.return_value = mock_response
            remote = SyncRemote(**get_sync_remote("team"))
            result = sync_push(source, remote)

        assert result.conversations == 1
        assert not result.dry_run
        client_instance.post.assert_called_once()
        call_kwargs = client_instance.post.call_args
        assert "/v1/push" in call_kwargs.args[0] or "/v1/push" in str(call_kwargs)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_sync.py::TestHTTPPush -v`
Expected: FAIL

**Step 3: Add URL detection helper and HTTP push transport**

In `sync.py`, add helper near top:

```python
def _is_http_remote(remote: SyncRemote) -> bool:
    """Check if remote uses HTTP transport (URL-based detection)."""
    return remote.path.startswith(("http://", "https://"))
```

Update `sync_push()` branching (line 96-99):

```python
        if _is_http_remote(remote):
            remote_existed = _push_http(remote, slice_path)
        elif remote.host:
            remote_existed = _push_ssh(remote, slice_path)
        else:
            remote_existed = _push_local(remote, slice_path, db_path)
```

Add `_push_http()` after `_push_local()`:

```python
def _push_http(remote: SyncRemote, slice_path: Path) -> bool:
    """Push via HTTP POST to remote /v1/push endpoint.

    Returns whether remote DB already existed.
    """
    import httpx

    from siftd.api.auth import AuthError, acquire_token
    from siftd.config import get_sync_remote

    remote_cfg = get_sync_remote(remote.name)
    auth = remote_cfg.get("auth") if remote_cfg else None
    headers = {}
    try:
        token = acquire_token(auth)
        headers["Authorization"] = f"Bearer {token}"
    except AuthError:
        pass  # --no-auth server

    url = remote.path.rstrip("/") + "/v1/push"
    data = slice_path.read_bytes()

    try:
        with httpx.Client(timeout=300) as client:
            resp = client.post(
                url, content=data,
                headers={**headers, "Content-Type": "application/octet-stream"},
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise SyncError(f"Push to {remote.path} failed: HTTP {e.response.status_code}") from e
    except httpx.ConnectError as e:
        raise SyncError(f"Cannot connect to {remote.path}: {e}") from e

    body = resp.json()
    return body.get("status") != "created"
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_sync.py::TestHTTPPush -v`
Expected: PASS

**Step 5: Write failing test — HTTP pull**

```python
class TestHTTPPull:
    def test_pull_http_streams_slice(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        (tmp_path / "config").mkdir()
        local_db = tmp_path / "local.db"

        # Create a small valid SQLite DB to use as the pull response body
        source = _make_db(
            tmp_path / "remote-slice.db",
            conversations=[{"external_id": "remote-conv-1"}],
        )
        slice_bytes = source.read_bytes()

        set_sync_remote("team", None, "https://siftd.example.com")
        set_remote_auth("team", {"token": "test-token"})

        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = slice_bytes
        mock_response.headers = {
            "X-Siftd-Conversations": "1",
            "X-Siftd-Size": str(len(slice_bytes)),
        }
        mock_response.raise_for_status = MagicMock()

        with patch("siftd.api.sync.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.return_value = mock_response
            remote = SyncRemote(**get_sync_remote("team"))
            result = sync_pull(local_db, remote)

        assert result.conversations == 1
        assert local_db.exists()
```

**Step 6: Run test to verify it fails**

Run: `pytest tests/test_sync.py::TestHTTPPull -v`
Expected: FAIL

**Step 7: Implement `_pull_http()`**

Update `sync_pull()` branching (line 294-301):

```python
    if _is_http_remote(remote):
        conversations, size_bytes = _pull_http(
            remote, db_path, effective_since, workspace, dry_run,
        )
    elif remote.host:
        conversations, size_bytes = _pull_ssh(
            remote, db_path, effective_since, workspace, dry_run,
        )
    else:
        conversations, size_bytes = _pull_local(
            remote, db_path, effective_since, workspace, dry_run,
        )
```

Add `_pull_http()`:

```python
def _pull_http(
    remote: SyncRemote,
    local_db: Path,
    since: str | None,
    workspace: str | None,
    dry_run: bool,
) -> tuple[int, int]:
    """Pull via HTTP GET from remote /v1/pull endpoint.

    Returns (conversations, size_bytes).
    """
    import httpx

    from siftd.api.auth import AuthError, acquire_token
    from siftd.config import get_sync_remote

    remote_cfg = get_sync_remote(remote.name)
    auth = remote_cfg.get("auth") if remote_cfg else None
    headers = {}
    try:
        token = acquire_token(auth)
        headers["Authorization"] = f"Bearer {token}"
    except AuthError:
        pass

    url = remote.path.rstrip("/") + "/v1/pull"
    params = {}
    if since is not None:
        params["since"] = since
    if workspace is not None:
        params["workspace"] = workspace

    try:
        with httpx.Client(timeout=300) as client:
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise SyncError(f"Pull from {remote.path} failed: HTTP {e.response.status_code}") from e
    except httpx.ConnectError as e:
        raise SyncError(f"Cannot connect to {remote.path}: {e}") from e

    conversations = int(resp.headers.get("X-Siftd-Conversations", 0))
    if conversations == 0:
        return 0, 0

    size_bytes = len(resp.content)

    if dry_run:
        return conversations, size_bytes

    with tempfile.NamedTemporaryFile(
        prefix="siftd-pull-http-", suffix=".db", delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        tmp_path.write_bytes(resp.content)
        from siftd.api.receive import receive_database
        receive_database(tmp_path, local_db, rebuild_fts=True)
        return conversations, size_bytes
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
```

**Step 8: Run tests**

Run: `pytest tests/test_sync.py::TestHTTPPull -v`
Expected: PASS

**Step 9: Run full sync test suite**

Run: `pytest tests/test_sync.py -v`
Expected: All PASS (existing SSH/local tests unaffected)

**Step 10: Commit**

```bash
git add src/siftd/api/sync.py tests/test_sync.py
git commit -m "Add HTTP transport for push and pull"
```

---

### Task 4: Server app factory, health, and push endpoint

The core server — Litestar app with health check and the push endpoint that wraps `receive_database()`.

**Files:**
- Create: `src/siftd/serve/app.py`
- Create: `src/siftd/serve/routes.py`
- Modify: `src/siftd/serve/__init__.py`
- Modify: `tests/test_serve.py`

**Step 1: Write failing test — health endpoint**

In `tests/test_serve.py`:

```python
"""Tests for siftd serve — HTTP team sync server."""

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from litestar.testing import TestClient

from siftd.serve.app import create_app


class TestHealth:
    def test_health_returns_ok(self, tmp_path):
        from siftd.storage.sqlite import create_database
        db = tmp_path / "team.db"
        create_database(db)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "db_size_bytes" in body
        assert "conversations" in body
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_serve.py::TestHealth -v`
Expected: FAIL (no `serve.app` module)

**Step 3: Implement app factory and health route**

`src/siftd/serve/app.py`:

```python
"""Litestar application factory for siftd serve."""

from __future__ import annotations

from pathlib import Path

from litestar import Litestar

from siftd.serve.routes import health


def create_app(
    *,
    db_path: Path,
    auth_config: dict | None = None,
    fts_rebuild: str = "on_push",
) -> Litestar:
    """Create the Litestar application.

    Args:
        db_path: Path to the team SQLite database.
        auth_config: Auth config dict (None = no auth).
        fts_rebuild: FTS rebuild strategy ("on_push", "scheduled", "off").
    """
    from litestar.di import Provide

    async def provide_db_path() -> Path:
        return db_path

    async def provide_fts_rebuild() -> str:
        return fts_rebuild

    return Litestar(
        route_handlers=[health],
        dependencies={
            "db_path": Provide(provide_db_path),
            "fts_rebuild": Provide(provide_fts_rebuild),
        },
    )
```

`src/siftd/serve/routes.py`:

```python
"""Route handlers for siftd serve."""

from __future__ import annotations

from pathlib import Path

from litestar import get


@get("/v1/health", opt={"no_auth": True})
async def health(db_path: Path) -> dict:
    """Health check — returns DB status."""
    from siftd.storage.sqlite import open_database

    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    conversations = 0
    if db_path.exists():
        conn = open_database(db_path, read_only=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
            conversations = row[0]
        finally:
            conn.close()

    return {
        "status": "ok",
        "db_size_bytes": size_bytes,
        "conversations": conversations,
    }
```

**Step 4: Run test**

Run: `pytest tests/test_serve.py::TestHealth -v`
Expected: PASS

**Step 5: Write failing test — push endpoint**

```python
class TestPush:
    def test_push_creates_db(self, tmp_path):
        from siftd.api.slice import slice_database
        from siftd.storage.sqlite import create_database, get_or_create_harness, \
            get_or_create_workspace, insert_conversation, insert_prompt, \
            insert_prompt_content, insert_response, insert_response_content, \
            get_or_create_model, get_or_create_provider

        # Build a source DB and slice it
        source = tmp_path / "source.db"
        conn = create_database(source)
        h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
        w = get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
        m = get_or_create_model(conn, "gpt-4")
        p = get_or_create_provider(conn, "openai")
        cid = insert_conversation(conn, external_id="c1", harness_id=h,
                                   workspace_id=w, started_at="2024-01-15T10:00:00Z")
        pid = insert_prompt(conn, cid, "p1", "2024-01-15T10:00:00Z")
        insert_prompt_content(conn, pid, 0, "text", '{"text": "hello"}')
        rid = insert_response(conn, cid, pid, m, p, "r1", "2024-01-15T10:00:01Z",
                               input_tokens=10, output_tokens=5)
        insert_response_content(conn, rid, 0, "text", '{"text": "hi"}')
        conn.commit()
        conn.close()

        slice_path = tmp_path / "slice.db"
        slice_database(source, slice_path, rebuild_fts=False)
        slice_bytes = slice_path.read_bytes()

        team_db = tmp_path / "team.db"
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.post(
                "/v1/push",
                content=slice_bytes,
                headers={"Content-Type": "application/octet-stream"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "created"
        assert body["conversations"] >= 1
        assert team_db.exists()
```

**Step 6: Run test to verify it fails**

Run: `pytest tests/test_serve.py::TestPush -v`
Expected: FAIL (no push route)

**Step 7: Implement push route**

Add to `routes.py`:

```python
import tempfile

from litestar import post, Request, Response


@post("/v1/push")
async def push(request: Request, db_path: Path, fts_rebuild: str) -> dict:
    """Receive a pushed slice and merge into team DB."""
    body = await request.body()
    if len(body) < 16:
        return Response(content={"error": "empty or invalid slice"}, status_code=400)

    with tempfile.NamedTemporaryFile(
        prefix="siftd-serve-push-", suffix=".db", delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        tmp_path.write_bytes(body)
        from siftd.api.receive import receive_database
        rebuild_fts = fts_rebuild == "on_push"
        result = receive_database(tmp_path, db_path, rebuild_fts=rebuild_fts)
        return {
            "status": result["status"],
            "conversations": result["conversations"],
        }
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
```

Register `push` in `app.py` route_handlers list.

**Step 8: Run tests**

Run: `pytest tests/test_serve.py -v`
Expected: PASS

**Step 9: Commit**

```bash
git add src/siftd/serve/ tests/test_serve.py
git commit -m "Add server app factory with health and push endpoints"
```

---

### Task 5: Server pull endpoint

Stream a filtered slice from the team DB.

**Files:**
- Modify: `src/siftd/serve/routes.py`
- Modify: `src/siftd/serve/app.py`
- Modify: `tests/test_serve.py`

**Step 1: Write failing test — pull with filters**

```python
class TestPull:
    def test_pull_streams_slice(self, tmp_path):
        """Push data in, then pull it back out."""
        from siftd.api.slice import slice_database
        from siftd.storage.sqlite import create_database, get_or_create_harness, \
            get_or_create_workspace, insert_conversation, insert_prompt, \
            insert_prompt_content, insert_response, insert_response_content, \
            get_or_create_model, get_or_create_provider
        from siftd.api.receive import receive_database

        # Build team DB with one conversation
        source = tmp_path / "source.db"
        conn = create_database(source)
        h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
        w = get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
        m = get_or_create_model(conn, "gpt-4")
        p = get_or_create_provider(conn, "openai")
        cid = insert_conversation(conn, external_id="c1", harness_id=h,
                                   workspace_id=w, started_at="2024-01-15T10:00:00Z")
        pid = insert_prompt(conn, cid, "p1", "2024-01-15T10:00:00Z")
        insert_prompt_content(conn, pid, 0, "text", '{"text": "hello"}')
        rid = insert_response(conn, cid, pid, m, p, "r1", "2024-01-15T10:00:01Z",
                               input_tokens=10, output_tokens=5)
        insert_response_content(conn, rid, 0, "text", '{"text": "hi"}')
        conn.commit()
        conn.close()

        team_db = tmp_path / "team.db"
        slice_path = tmp_path / "slice.db"
        slice_database(source, slice_path, rebuild_fts=False)
        receive_database(slice_path, team_db)

        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/v1/pull")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "application/octet-stream"
        assert int(resp.headers["X-Siftd-Conversations"]) >= 1
        # Response body should be valid SQLite
        assert resp.content[:16].startswith(b"SQLite format 3")

    def test_pull_empty_db(self, tmp_path):
        from siftd.storage.sqlite import create_database
        team_db = tmp_path / "team.db"
        create_database(team_db)
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/v1/pull")
        assert resp.status_code == 200
        assert int(resp.headers.get("X-Siftd-Conversations", 0)) == 0
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_serve.py::TestPull -v`
Expected: FAIL

**Step 3: Implement pull route**

Add to `routes.py`:

```python
from litestar import get
from litestar.params import Parameter


@get("/v1/pull")
async def pull(
    db_path: Path,
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    model: str | None = Parameter(query="model", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
) -> Response:
    """Slice and stream the team DB based on filters."""
    from siftd.api.slice import slice_database

    with tempfile.TemporaryDirectory(prefix="siftd-serve-pull-") as tmp_dir:
        slice_path = Path(tmp_dir) / "pull-slice.db"
        result = slice_database(
            source_db=db_path,
            target_path=slice_path,
            workspace=workspace,
            since=since,
            before=before,
            model=model,
            tags=tag,
            rebuild_fts=False,
        )

        conversations = result["conversations"]
        if conversations == 0:
            return Response(
                content=b"",
                status_code=200,
                media_type="application/octet-stream",
                headers={
                    "X-Siftd-Conversations": "0",
                    "X-Siftd-Size": "0",
                },
            )

        data = slice_path.read_bytes()
        return Response(
            content=data,
            status_code=200,
            media_type="application/octet-stream",
            headers={
                "X-Siftd-Conversations": str(conversations),
                "X-Siftd-Size": str(len(data)),
            },
        )
```

Register `pull` in `app.py` route_handlers list.

**Step 4: Run tests**

Run: `pytest tests/test_serve.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/siftd/serve/ tests/test_serve.py
git commit -m "Add pull endpoint with full filter support"
```

---

### Task 6: Server search & query endpoints

Read-only endpoints that expose existing search and query against the team DB.

**Files:**
- Modify: `src/siftd/serve/routes.py`
- Modify: `src/siftd/serve/app.py`
- Modify: `tests/test_serve.py`

**Step 1: Write failing test — query endpoint**

```python
class TestQuery:
    def test_query_lists_conversations(self, tmp_path):
        """Query returns conversations from team DB."""
        # (reuse the pattern from TestPull to build a team DB with data)
        # ... setup team_db with one conversation ...
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/v1/query")
        assert resp.status_code == 200
        body = resp.json()
        assert "conversations" in body
        assert len(body["conversations"]) >= 1

    def test_query_single_conversation(self, tmp_path):
        # ... setup team_db ...
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/v1/query", params={"n": 1})
        body = resp.json()
        assert len(body["conversations"]) == 1
```

**Step 2: Run test, verify fail**

**Step 3: Implement query route**

The query route wraps `list_conversations()` from `storage/sqlite.py` and `WhereBuilder` from `storage/query.py`. Read `cli_query.py` for the exact calling pattern. The route accepts `FilterArgs`-compatible query params and returns JSON.

```python
@get("/v1/query")
async def query(
    db_path: Path,
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    model: str | None = Parameter(query="model", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
    search: str | None = Parameter(query="search", default=None),
    n: int = Parameter(query="n", default=20),
    id: str | None = Parameter(query="id", default=None),
) -> dict:
    """List or detail conversations."""
    from siftd.storage.sqlite import open_database

    conn = open_database(db_path, read_only=True)
    try:
        if id is not None:
            # Single conversation detail
            from siftd.storage.query import conversation_detail
            detail = conversation_detail(conn, id)
            return {"conversation": detail}

        from siftd.storage.query import list_conversations, WhereBuilder
        wb = WhereBuilder()
        if workspace:
            wb.workspace(workspace)
        if since:
            wb.since(since)
        if before:
            wb.before(before)
        if model:
            wb.model(model)
        if tag:
            for t in tag:
                wb.tag(t)
        if search:
            wb.fts(search)
        rows = list_conversations(conn, where=wb, limit=n)
        return {"conversations": [dict(r) for r in rows]}
    finally:
        conn.close()
```

**Note:** Check the actual `WhereBuilder` API in `storage/query.py` — adjust method names to match the real interface.

**Step 4: Run tests, verify pass**

**Step 5: Write failing test — search endpoint**

Search requires embeddings, so mark test accordingly:

```python
@pytest.mark.embeddings
class TestSearch:
    def test_search_returns_results(self, tmp_path):
        # Build team DB with embedded data...
        # This may need embeddings setup — check if hybrid_search
        # gracefully degrades without embeddings or skip with marker
        pass
```

**Note:** The search endpoint calls `hybrid_search()` which requires `siftd[embed]`. If embeddings aren't available on the server, search returns 501. Test with the marker.

**Step 6: Implement search route**

```python
@get("/v1/search")
async def search_route(
    db_path: Path,
    q: str = Parameter(query="q"),
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    model: str | None = Parameter(query="model", default=None),
    threshold: float = Parameter(query="threshold", default=0.0),
    n: int = Parameter(query="n", default=10),
) -> dict | Response:
    """Semantic + FTS search against team DB."""
    try:
        from siftd.search import hybrid_search
    except ImportError:
        return Response(
            content={"error": "search requires siftd[embed]"},
            status_code=501,
        )

    results = hybrid_search(
        q, db_path=db_path, limit=n,
        workspace=workspace, model=model,
        since=since, before=before,
    )

    if threshold > 0:
        results = [r for r in results if r.get("score", 0) >= threshold]

    return {
        "query": q,
        "result_count": len(results),
        "results": results,
    }
```

Register both routes in `app.py`.

**Step 7: Run full test suite**

Run: `pytest tests/test_serve.py -v`
Expected: PASS (search test may be skipped if no embed)

**Step 8: Commit**

```bash
git add src/siftd/serve/ tests/test_serve.py
git commit -m "Add query and search endpoints"
```

---

### Task 7: Auth middleware — OIDC and introspection

Two auth modes as Litestar middleware. `--no-auth` (auth_config=None) skips validation.

**Files:**
- Create: `src/siftd/serve/auth.py`
- Modify: `src/siftd/serve/app.py`
- Modify: `tests/test_serve.py`

**Step 1: Write failing test — no-auth mode passes all requests**

```python
class TestAuthNoAuth:
    def test_no_auth_allows_all(self, tmp_path):
        from siftd.storage.sqlite import create_database
        db = tmp_path / "team.db"
        create_database(db)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/v1/health")
            assert resp.status_code == 200
```

**Step 2: Write failing test — missing bearer token returns 401**

```python
class TestAuthOIDC:
    def test_missing_token_returns_401(self, tmp_path):
        from siftd.storage.sqlite import create_database
        db = tmp_path / "team.db"
        create_database(db)
        auth_config = {"issuer": "https://example.com", "audience": "siftd"}
        app = create_app(db_path=db, auth_config=auth_config)
        with TestClient(app) as client:
            resp = client.post("/v1/push", content=b"x" * 100)
            assert resp.status_code == 401

    def test_health_bypasses_auth(self, tmp_path):
        from siftd.storage.sqlite import create_database
        db = tmp_path / "team.db"
        create_database(db)
        auth_config = {"issuer": "https://example.com", "audience": "siftd"}
        app = create_app(db_path=db, auth_config=auth_config)
        with TestClient(app) as client:
            resp = client.get("/v1/health")
            assert resp.status_code == 200  # health skips auth
```

**Step 3: Implement auth middleware**

`src/siftd/serve/auth.py`:

```python
"""Authentication middleware for siftd serve.

Supports two modes:
- OIDC: JWT validation against a configurable issuer's JWKS
- Introspection: RFC 7662 token introspection
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from litestar.connection import ASGIConnection
from litestar.middleware import AbstractAuthenticationMiddleware, AuthenticationResult


@dataclass
class UserIdentity:
    """Authenticated user from token validation."""
    sub: str  # subject / identity string


class SiftdAuthMiddleware(AbstractAuthenticationMiddleware):
    """Bearer token authentication middleware."""

    def __init__(self, app, auth_config: dict) -> None:
        super().__init__(app)
        self._config = auth_config
        self._jwks_cache: dict | None = None
        self._jwks_fetched_at: float = 0
        self._introspection_cache: dict[str, tuple[dict, float]] = {}

    async def authenticate_request(
        self, connection: ASGIConnection,
    ) -> AuthenticationResult:
        # Skip auth for health endpoint
        if connection.scope.get("path", "").endswith("/health"):
            return AuthenticationResult(user=UserIdentity(sub="anonymous"), auth=None)

        # Check opt-out on route
        handler = connection.scope.get("route_handler")
        if handler and getattr(handler, "opt", {}).get("no_auth"):
            return AuthenticationResult(user=UserIdentity(sub="anonymous"), auth=None)

        auth_header = connection.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise NotAuthenticated("Missing bearer token")

        token = auth_header[7:]

        if "issuer" in self._config:
            identity = await self._validate_oidc(token)
        elif "introspection_url" in self._config:
            identity = await self._validate_introspection(token)
        else:
            raise NotAuthenticated("No auth mode configured")

        return AuthenticationResult(user=identity, auth=token)

    async def _validate_oidc(self, token: str) -> UserIdentity:
        """Validate JWT against OIDC issuer's JWKS."""
        import jwt

        jwks = await self._get_jwks()
        identity_claim = self._config.get("identity_claim", "sub")
        audience = self._config.get("audience", "siftd")

        try:
            payload = jwt.decode(
                token, jwks,
                algorithms=["RS256", "ES256"],
                audience=audience,
            )
            return UserIdentity(sub=payload.get(identity_claim, payload.get("sub", "unknown")))
        except jwt.PyJWTError as e:
            raise NotAuthenticated(f"Invalid token: {e}") from e

    async def _validate_introspection(self, token: str) -> UserIdentity:
        """Validate token via RFC 7662 introspection endpoint."""
        import httpx

        # Check cache (60s TTL)
        now = time.time()
        if token in self._introspection_cache:
            cached, cached_at = self._introspection_cache[token]
            if now - cached_at < 60:
                identity_claim = self._config.get("identity_claim", "username")
                return UserIdentity(sub=cached.get(identity_claim, "unknown"))

        url = self._config["introspection_url"]
        client_id = self._config.get("client_id", "")
        client_secret = self._config.get("client_secret", "")

        # Resolve env: prefix on client_secret
        if client_secret.startswith("env:"):
            import os
            client_secret = os.environ.get(client_secret[4:], "")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                data={"token": token},
                auth=(client_id, client_secret) if client_id else None,
            )

        if resp.status_code != 200:
            raise NotAuthenticated("Introspection request failed")

        body = resp.json()
        if not body.get("active", False):
            raise NotAuthenticated("Token is not active")

        self._introspection_cache[token] = (body, now)
        identity_claim = self._config.get("identity_claim", "username")
        return UserIdentity(sub=body.get(identity_claim, "unknown"))

    async def _get_jwks(self):
        """Fetch and cache JWKS from OIDC issuer."""
        import httpx
        import jwt

        now = time.time()
        if self._jwks_cache and now - self._jwks_fetched_at < 3600:
            return self._jwks_cache

        issuer = self._config["issuer"].rstrip("/")
        jwks_url = self._config.get("jwks_url")

        if not jwks_url:
            async with httpx.AsyncClient() as client:
                disco = await client.get(f"{issuer}/.well-known/openid-configuration")
                jwks_url = disco.json()["jwks_uri"]

        async with httpx.AsyncClient() as client:
            resp = await client.get(jwks_url)

        self._jwks_cache = jwt.PyJWKSet.from_dict(resp.json())
        self._jwks_fetched_at = now
        return self._jwks_cache


class NotAuthenticated(Exception):
    """Raised when authentication fails."""
```

Wire into `app.py`:

```python
from litestar.middleware.base import DefineMiddleware

def create_app(...):
    middleware = []
    if auth_config:
        from siftd.serve.auth import SiftdAuthMiddleware
        middleware.append(
            DefineMiddleware(SiftdAuthMiddleware, auth_config=auth_config)
        )

    return Litestar(
        route_handlers=[health, push, pull, query, search_route],
        middleware=middleware,
        ...
    )
```

**Note:** The exact Litestar middleware API may need adjustment — check Litestar docs for `AbstractAuthenticationMiddleware` vs `MiddlewareProtocol`. The pattern above is directional; adapt to Litestar's actual API during implementation.

**Step 4: Run tests**

Run: `pytest tests/test_serve.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/siftd/serve/ tests/test_serve.py
git commit -m "Add auth middleware with OIDC and introspection modes"
```

---

### Task 8: Push log & attribution

Audit trail for pushes. Tag incoming conversations with `pushed_by:<identity>`.

**Files:**
- Modify: `src/siftd/storage/sqlite.py` (~line 82)
- Modify: `src/siftd/serve/routes.py` (push handler)
- Modify: `tests/test_serve.py`

**Step 1: Write failing test — push log recorded**

```python
class TestAttribution:
    def test_push_records_push_log(self, tmp_path):
        """Push with identity records to push_log table."""
        # Build slice, push to team DB with auth identity
        # ... (build source, slice it) ...

        team_db = tmp_path / "team.db"
        app = create_app(db_path=team_db, auth_config=None)

        with TestClient(app) as client:
            resp = client.post(
                "/v1/push",
                content=slice_bytes,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Siftd-Identity": "alice",  # passed by auth middleware
                },
            )
        assert resp.status_code == 200

        import sqlite3
        conn = sqlite3.connect(str(team_db))
        rows = conn.execute("SELECT * FROM push_log").fetchall()
        conn.close()
        assert len(rows) == 1
```

**Step 2: Run test, verify fail**

**Step 3: Add `ensure_push_log_table()` migration**

In `storage/sqlite.py`, add after `_ensure_tag_indexes()`:

```python
def ensure_push_log_table(conn: sqlite3.Connection) -> None:
    """Create push_log table for server attribution."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_log (
            push_id TEXT PRIMARY KEY,
            user_identity TEXT NOT NULL,
            pushed_at TEXT NOT NULL,
            conversations INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            source_ip TEXT
        )
    """)
```

**Don't call this from `open_database()`** — it's server-only. Call it from `create_app()` or the push handler.

**Step 4: Update push handler to record attribution**

In `routes.py`, update the push handler to:
1. Extract identity from `request.user.sub` (set by auth middleware) or "anonymous"
2. After `receive_database()`, insert push_log row
3. Tag incoming conversations with `pushed_by:<identity>`

```python
@post("/v1/push")
async def push(request: Request, db_path: Path, fts_rebuild: str) -> dict:
    body = await request.body()
    # ... existing receive logic ...

    # Attribution
    identity = getattr(getattr(request, "user", None), "sub", "anonymous")
    from siftd.ids import ulid
    from siftd.storage.sqlite import open_database, ensure_push_log_table

    conn = open_database(db_path)
    try:
        ensure_push_log_table(conn)
        conn.execute(
            "INSERT INTO push_log (push_id, user_identity, pushed_at, conversations, size_bytes, source_ip) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ulid(), identity, datetime.now(UTC).isoformat(),
             result["conversations"], len(body),
             request.client.host if request.client else None),
        )

        # Tag conversations with pushed_by:<identity>
        if identity != "anonymous":
            from siftd.storage.tags import tag_conversations_by_push
            # ... or use existing tag_conversation() for each new conversation
            pass

        conn.commit()
    finally:
        conn.close()

    return {"status": result["status"], "conversations": result["conversations"]}
```

**Note:** The exact tagging mechanism needs to resolve which conversations were just pushed. One approach: `receive_database()` returns the list of conversation IDs it inserted, then tag those. This may require extending `receive_database()` or `merge_database()` to return inserted IDs. Evaluate during implementation — if it's complex, defer tagging to a follow-up and ship push_log first.

**Step 5: Run tests**

Run: `pytest tests/test_serve.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add src/siftd/storage/sqlite.py src/siftd/serve/ tests/test_serve.py
git commit -m "Add push log attribution and pushed_by tagging"
```

---

### Task 9: CLI — `siftd serve` command

Wire the server into the CLI.

**Files:**
- Create: `src/siftd/cli_serve.py`
- Modify: `src/siftd/cli.py:1-47`

**Step 1: Write failing test — CLI invocation**

```python
def test_serve_help(capsys):
    """siftd serve --help exits cleanly."""
    from siftd.cli import main
    with pytest.raises(SystemExit) as exc:
        main(["serve", "--help"])
    assert exc.value.code == 0
```

**Step 2: Run test, verify fail**

**Step 3: Implement `cli_serve.py`**

```python
"""CLI dispatcher for siftd serve."""

from __future__ import annotations


def cmd_serve(args) -> int:
    """Start the HTTP team sync server."""
    from siftd.serve import require_serve

    require_serve()

    from pathlib import Path

    from siftd.config import get_config
    from siftd.serve.app import create_app

    # Resolve DB path: CLI arg > config > default
    db_path = getattr(args, "db", None)
    if db_path:
        db_path = Path(db_path)
    else:
        db_config = get_config("serve.db")
        if db_config:
            db_path = Path(db_config)
        else:
            from siftd.paths import db_path as default_db_path
            db_path = default_db_path()

    host = getattr(args, "host", None) or get_config("serve.host") or "0.0.0.0"
    port = int(getattr(args, "port", None) or get_config("serve.port") or 8484)
    fts_rebuild = get_config("serve.fts_rebuild") or "on_push"

    # Auth config
    auth_config = None
    if not args.no_auth:
        auth_section = get_config("serve.auth")
        if isinstance(auth_section, dict):
            auth_config = dict(auth_section)

    app = create_app(db_path=db_path, auth_config=auth_config, fts_rebuild=fts_rebuild)

    import uvicorn
    print(f"siftd serve — listening on {host}:{port}")
    print(f"  db: {db_path}")
    print(f"  auth: {'enabled' if auth_config else 'disabled (--no-auth)'}")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def build_serve_parser(subparsers) -> None:
    """Register the serve subcommand."""
    parser = subparsers.add_parser(
        "serve",
        help="Start the HTTP team sync server",
        description="Serve the siftd database over HTTP for team sync.",
    )
    parser.add_argument(
        "--host", metavar="ADDR",
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port", metavar="PORT", type=int,
        help="Listen port (default: 8484)",
    )
    parser.add_argument(
        "--no-auth", action="store_true",
        help="Disable authentication (development only)",
    )
    parser.set_defaults(func=cmd_serve)
```

**Step 4: Register in `cli.py`**

Add import (after line 9, alphabetical):

```python
from siftd.cli_serve import build_serve_parser
```

Add call (after line 47):

```python
build_serve_parser(subparsers)
```

**Note:** This import is eager (top-level in cli.py, matching the existing pattern). The `require_serve()` guard is inside `cmd_serve()`, so `siftd serve` fails with a helpful message if litestar isn't installed, but other commands aren't affected.

**Step 5: Run tests**

Run: `./dev check`
Expected: PASS

**Step 6: Commit**

```bash
git add src/siftd/cli_serve.py src/siftd/cli.py
git commit -m "Add siftd serve CLI command"
```

---

### Task 10: Documentation

Concept doc covering setup, deployment, and usage.

**Files:**
- Create: `docs/concepts/serve.md`

**Step 1: Write the concept doc**

`docs/concepts/serve.md` should cover:

1. **What it is** — HTTP server wrapping existing siftd primitives for team sync
2. **When you'd use it** — team shared DB, remote search, cloud/VM deployment
3. **Server setup** — config.toml `[serve]` section, auth modes
4. **Client setup** — `db remote add team https://...`, auth config
5. **Push/pull workflow** — same commands, HTTP transport auto-detected
6. **Deployment** — Docker one-liner, systemd unit, reverse proxy for TLS
7. **Attribution** — push log, `pushed_by:` tags, querying by author
8. **Relationship to SSH sync** — HTTP for team, SSH for personal homelab, both coexist

Keep it concise and workflow-oriented (like `docs/concepts/sync.md`).

**Step 2: Run doc check**

Run: `./dev docs --check`
Expected: PASS (or update reference docs if needed)

**Step 3: Commit**

```bash
git add docs/concepts/serve.md
git commit -m "Add serve concept documentation"
```

**Step 4: Run full check**

Run: `./dev check`
Expected: PASS

---

## Implementation order & dependencies

```
Task 1 (scaffolding)
  └─→ Task 2 (config + token)
       └─→ Task 3 (HTTP transport)
  └─→ Task 4 (server + health + push)
       └─→ Task 5 (pull endpoint)
       └─→ Task 6 (search + query)
       └─→ Task 7 (auth middleware)
       └─→ Task 8 (push log + attribution)
  └─→ Task 9 (CLI — depends on Task 4)
  └─→ Task 10 (docs — last, after all features)
```

Tasks 2-3 (client) and Tasks 4-8 (server) can be worked in parallel after Task 1.
Task 9 depends on the server app existing (Task 4).
Task 10 is last.

## Notes for implementor

- **Litestar API:** Check Litestar docs for exact middleware, DI, and Response APIs. The plan shows the intent; adapt class names and signatures to match the version installed.
- **`hybrid_search()` requires embeddings:** The search endpoint should return 501 if `siftd[embed]` is not installed on the server. Test with `@pytest.mark.embeddings`.
- **Attribution tagging:** If extending `receive_database()` to return inserted conversation IDs is complex, ship push_log first and add tagging as a follow-up.
- **SyncRemote stays unchanged:** No new fields. HTTP detection is purely from `remote.path` URL prefix. The `auth` config lives in the config dict, not the dataclass.
- **`cli.py` import:** `build_serve_parser` import is at the top of `cli.py` (matches existing pattern). It will fail if `cli_serve.py` doesn't exist, so both files must be created together.
- **Test isolation:** Each test creates its own temp DB. No shared fixtures. Monkeypatch `XDG_CONFIG_HOME` for config tests.
