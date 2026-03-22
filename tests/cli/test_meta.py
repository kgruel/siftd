"""Tests for siftd.cli.meta command handlers."""

from types import SimpleNamespace

from siftd.cli.meta import cmd_adapters, cmd_config, cmd_path, cmd_status, cmd_workspaces


def _args(**kwargs):
    base = {
        "db": None,
        "json": False,
        "limit": 10,
        "action": None,
        "key": None,
        "value": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _stats_obj(tmp_path):
    return SimpleNamespace(
        db_path=tmp_path / "db.sqlite",
        db_size_bytes=2048,
        counts=SimpleNamespace(
            conversations=3,
            prompts=4,
            responses=5,
            tool_calls=1,
            harnesses=1,
            workspaces=1,
            tools=2,
            models=2,
            ingested_files=6,
        ),
        harnesses=[SimpleNamespace(name="claude", source="local", log_format="jsonl")],
        harness_counts=[SimpleNamespace(name="claude", conversation_count=3)],
        top_workspaces=[SimpleNamespace(path="/w", conversation_count=3, last_activity="2024-01-01")],
        models=["m1"],
        top_tools=[SimpleNamespace(name="bash", usage_count=2)],
        token_coverage=SimpleNamespace(
            responses=5,
            with_tokens=4,
            pct_with_tokens=80.0,
            by_harness=[SimpleNamespace(name="claude", responses=5, with_tokens=4, pct_with_tokens=80.0)],
        ),
        top_tags=[SimpleNamespace(name="research", count=2)],
        activity_window=("2024-01-01", "2024-01-02"),
        last_ingest_at="2024-01-03",
    )


def test_cmd_path(monkeypatch, capsys):
    monkeypatch.setattr("siftd.cli.meta.data_dir", lambda: "/d")
    monkeypatch.setattr("siftd.cli.meta.config_dir", lambda: "/c")
    monkeypatch.setattr("siftd.cli.meta.cache_dir", lambda: "/k")
    monkeypatch.setattr("siftd.cli.meta.db_path", lambda: "/db")
    assert cmd_path(_args()) == 0
    out = capsys.readouterr().out
    assert "Data directory" in out and "/db" in out


def test_cmd_config_path_get_set(monkeypatch, capsys):
    monkeypatch.setattr("siftd.cli.meta.config_file", lambda: "/tmp/config.yaml")
    assert cmd_config(_args(action="path")) == 0

    monkeypatch.setattr("siftd.config.get_config", lambda key: None)
    assert cmd_config(_args(action="get", key="x.y")) == 1

    monkeypatch.setattr("siftd.config.get_config", lambda key: "v")
    assert cmd_config(_args(action="get", key="x.y")) == 0

    monkeypatch.setattr("siftd.config.set_config", lambda k, v: None)
    monkeypatch.setattr("siftd.config.get_config", lambda key: "stored")
    assert cmd_config(_args(action="set", key="x.y", value="z")) == 0


def test_cmd_config_append_remove_and_show(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("search:\n  formatter: verbose\n")

    monkeypatch.setattr("siftd.cli.meta.config_file", lambda: cfg)
    monkeypatch.setattr("siftd.config.append_config_list", lambda k, v: True)
    assert cmd_config(_args(action="append", key="a", value="b")) == 0

    monkeypatch.setattr("siftd.config.remove_config_list", lambda k, v: False)
    assert cmd_config(_args(action="remove", key="a", value="b")) == 1

    monkeypatch.setattr("siftd.config.load_config", lambda: {"search": {"formatter": "verbose"}})
    monkeypatch.setattr("siftd.config._validate_config", lambda doc: None)
    assert cmd_config(_args()) == 0
    assert "formatter" in capsys.readouterr().out


def test_cmd_adapters_json_and_table(monkeypatch, capsys):
    monkeypatch.setattr("siftd.api.list_adapters", lambda: [])
    assert cmd_adapters(_args(json=True)) == 0

    rows = [SimpleNamespace(name="claude", origin="builtin", locations=["~/.claude"], source_path="x", entrypoint="y")]
    monkeypatch.setattr("siftd.api.list_adapters", lambda: rows)
    assert cmd_adapters(_args(json=True)) == 0

    printed = []
    monkeypatch.setattr("siftd.output.print_table", lambda h, r: printed.append((h, r)))
    assert cmd_adapters(_args(json=False)) == 0
    assert printed


def test_cmd_workspaces_and_status(monkeypatch, tmp_path, capsys):
    stats = _stats_obj(tmp_path)

    monkeypatch.setattr("siftd.api.stats.read_stats_cache", lambda **k: None)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: stats)
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: False)

    assert cmd_status(_args(json=True, db=str(tmp_path / "db.sqlite"))) == 0
    assert '"features"' in capsys.readouterr().out

    assert cmd_status(_args(json=False, db=str(tmp_path / "db.sqlite"))) == 0
    out = capsys.readouterr().out
    assert "Database:" in out and "Embeddings:" in out

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: [{"path": "/w", "convs": 2, "last_activity": "2024-01-01"}])
    assert cmd_workspaces(_args(json=True, db=str(tmp_path / "db.sqlite"), limit=5)) == 0

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: [])
    assert cmd_workspaces(_args(json=False, db=str(tmp_path / "db.sqlite"), limit=5)) == 0
    assert "No workspaces" in capsys.readouterr().out


def test_status_and_workspaces_remaining_branches(monkeypatch, tmp_path, capsys):
    # status: serve delegation result path + dict conversion
    stats = _stats_obj(tmp_path)
    stats.activity_window = ("2024-01-01", None)
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: {"ok": 1})
    monkeypatch.setattr("siftd.api.stats._dict_to_stats", lambda d: stats)
    monkeypatch.setattr("siftd.api.stats.read_stats_cache", lambda **k: None)
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: True)
    assert cmd_status(_args(json=False, db=str(tmp_path / "db.sqlite"))) == 0
    out = capsys.readouterr().out
    assert "(unknown)" in out and "Embeddings: installed" in out

    # status: execute FileNotFoundError branch
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
    monkeypatch.setattr("siftd.api.stats.read_stats_cache", lambda **k: None)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (_ for _ in ()).throw(FileNotFoundError("missing")))
    assert cmd_status(_args(json=False, db=str(tmp_path / "db.sqlite"))) == 1

    # workspaces: serve delegation rows branch
    monkeypatch.setattr(
        "siftd.serve.delegation.try_serve",
        lambda op: {"workspaces": [{"path": "/w", "conversations": 2, "last_activity": None}]},
    )
    assert cmd_workspaces(_args(json=False, db=str(tmp_path / "db.sqlite"), limit=1)) == 0

    # workspaces: execute FileNotFoundError for json and non-json
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (_ for _ in ()).throw(FileNotFoundError("missing")))
    assert cmd_workspaces(_args(json=True, db=str(tmp_path / "db.sqlite"), limit=1)) == 0
    assert cmd_workspaces(_args(json=False, db=str(tmp_path / "db.sqlite"), limit=1)) == 1


def test_config_and_adapters_remaining_branches(monkeypatch, tmp_path, capsys):
    # config usage and validation errors
    assert cmd_config(_args(action="get", key=None)) == 1
    assert cmd_config(_args(action="set", key=None, value="x")) == 1

    monkeypatch.setattr("siftd.config.set_config", lambda k, v: (_ for _ in ()).throw(ValueError("bad")))
    assert cmd_config(_args(action="set", key="a", value="b")) == 1

    assert cmd_config(_args(action="append", key=None, value="x")) == 1
    monkeypatch.setattr("siftd.config.append_config_list", lambda k, v: (_ for _ in ()).throw(ValueError("bad")))
    assert cmd_config(_args(action="append", key="a", value="b")) == 1
    monkeypatch.setattr("siftd.config.append_config_list", lambda k, v: False)
    assert cmd_config(_args(action="append", key="a", value="b")) == 0

    assert cmd_config(_args(action="remove", key=None, value="x")) == 1
    monkeypatch.setattr("siftd.config.remove_config_list", lambda k, v: (_ for _ in ()).throw(ValueError("bad")))
    assert cmd_config(_args(action="remove", key="a", value="b")) == 1
    monkeypatch.setattr("siftd.config.remove_config_list", lambda k, v: True)
    assert cmd_config(_args(action="remove", key="a", value="b")) == 0

    # config show missing file
    missing = tmp_path / "missing.toml"
    monkeypatch.setattr("siftd.cli.meta.config_file", lambda: missing)
    assert cmd_config(_args()) == 0

    # adapters no rows (non-json)
    monkeypatch.setattr("siftd.api.list_adapters", lambda: [])
    assert cmd_adapters(_args(json=False)) == 0
    assert "No adapters found" in capsys.readouterr().out
