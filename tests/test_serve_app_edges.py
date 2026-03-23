import asyncio

from siftd.serve import app as serve_app


def test_create_app_without_auth_has_no_middleware(tmp_path):
    app = serve_app.create_app(db_path=tmp_path / "db.db", auth_config=None, fts_rebuild="off")
    assert app is not None
    assert app.middleware == []


def test_create_app_with_auth_adds_middleware_and_dependencies(monkeypatch, tmp_path):
    def marker(app):
        return app

    monkeypatch.setattr("siftd.serve.auth.create_auth_middleware", lambda cfg: marker)

    db = tmp_path / "team.db"
    app = serve_app.create_app(db_path=db, auth_config={"issuer": "https://idp"}, fts_rebuild="scheduled")

    assert app.middleware == [marker]
    provide_db = app.dependencies["db_path"].dependency
    provide_fts = app.dependencies["fts_rebuild"].dependency
    assert asyncio.run(provide_db()) == db
    assert asyncio.run(provide_fts()) == "scheduled"
