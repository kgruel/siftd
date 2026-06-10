import asyncio

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from siftd.serve import app as serve_app


def test_create_app_without_auth_has_no_middleware(tmp_path):
    # rate_limit_per_minute=0 isolates the auth-middleware wiring under test;
    # the rate limiter (F4, on by default) would otherwise add its own entry.
    app = serve_app.create_app(
        db_path=tmp_path / "db.db", auth_config=None, fts_rebuild="off",
        rate_limit_per_minute=0,
    )
    assert app.middleware == []


def test_create_app_with_auth_adds_middleware_and_dependencies(monkeypatch, tmp_path):
    def marker(app):
        return app

    monkeypatch.setattr("siftd.serve.auth.create_auth_middleware", lambda _cfg: marker)
    db = tmp_path / "team.db"
    app = serve_app.create_app(
        db_path=db, auth_config={"issuer": "https://idp"}, fts_rebuild="scheduled",
        rate_limit_per_minute=0,
    )
    assert app.middleware == [marker]
    assert (asyncio.run(app.dependencies["db_path"].dependency()), asyncio.run(app.dependencies["fts_rebuild"].dependency())) == (db, "scheduled")


def test_ui_shell_public_but_data_routes_require_auth(tmp_path):
    """When auth is enabled, /ui (shell) is public but /ui/query returns 401."""
    from litestar.testing import TestClient

    from siftd.storage.sqlite import create_database

    db = tmp_path / "team.db"
    create_database(db)
    auth_config = {"issuer": "https://example.com", "audience": "siftd"}
    app = serve_app.create_app(db_path=db, auth_config=auth_config)
    with TestClient(app, raise_server_exceptions=False) as client:
        # Shell is public (no_auth)
        assert client.get("/").status_code == 200
        # Data routes require auth
        assert client.get("/query").status_code == 401
        assert client.get("/stats").status_code == 401
        assert client.get("/search").status_code == 401
