import asyncio

from siftd.serve import app as serve_app


def test_create_app_without_auth_has_no_middleware(tmp_path):
    assert serve_app.create_app(db_path=tmp_path / "db.db", auth_config=None, fts_rebuild="off").middleware == []


def test_create_app_with_auth_adds_middleware_and_dependencies(monkeypatch, tmp_path):
    def marker(app):
        return app

    monkeypatch.setattr("siftd.serve.auth.create_auth_middleware", lambda _cfg: marker)
    db = tmp_path / "team.db"
    app = serve_app.create_app(db_path=db, auth_config={"issuer": "https://idp"}, fts_rebuild="scheduled")
    assert app.middleware == [marker]
    assert asyncio.run(app.dependencies["db_path"].dependency()) == db
    assert asyncio.run(app.dependencies["fts_rebuild"].dependency()) == "scheduled"
