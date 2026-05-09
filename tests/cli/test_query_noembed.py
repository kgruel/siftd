"""Additional no-embed tests for siftd.cli.query branches."""

from types import SimpleNamespace

from siftd.cli.query import _query_detail, _query_sql, cmd_query


def _args(**kwargs):
    base = {
        "db": None,
        "json": False,
        "limit": 10,
        "conversation_id": None,
        "sql_name": None,
        "var": None,
        "workspace": None,
        "model": None,
        "since": None,
        "before": None,
        "tool": None,
        "tag": None,
        "all_tags": None,
        "no_tag": None,
        "tool_tag": None,
        "oldest": False,
        "verbose": False,
        "stats": False,
        "no_hints": False,
        "exchanges": None,
        "brief": False,
        "summary": False,
        "full": False,
        "chars": None,
        "thinking": False,
        "tools": None,
        "tool_chars": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_query_detail_branches(monkeypatch, capsys, tmp_path):
    # invalid exchanges
    assert _query_detail(_args(conversation_id="c1", exchanges=0, db=str(tmp_path / "db.sqlite"))) == 1

    detail = SimpleNamespace(
        id="c1",
        workspace_path="/w",
        started_at="2024-01-01",
        model="m",
        total_input_tokens=10,
        total_output_tokens=20,
        tags=["a"],
        turns=[{"x": 1}, {"x": 2}],
    )

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: detail)
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: {"conversation": {"id": "c1"}})
    assert _query_detail(_args(conversation_id="c1", json=True, db=str(tmp_path / "db.sqlite"))) == 0
    assert '"id": "c1"' in capsys.readouterr().out

    # file missing / not found
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (_ for _ in ()).throw(FileNotFoundError("missing")))
    assert _query_detail(_args(conversation_id="c1", db=str(tmp_path / "db.sqlite"))) == 1

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: None)
    assert _query_detail(_args(conversation_id="c1", db=str(tmp_path / "db.sqlite"))) == 1

    # summary and render path
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: detail)
    assert _query_detail(_args(conversation_id="c1", summary=True, db=str(tmp_path / "db.sqlite"))) == 0

    monkeypatch.setattr("siftd.output.format_registry.select_format", lambda **k: SimpleNamespace(render_detail=lambda *a, **k2: "OUT"))
    monkeypatch.setattr("siftd.output.painted_bridge.emit_output", lambda out: None)
    assert _query_detail(_args(conversation_id="c1", exchanges=1, db=str(tmp_path / "db.sqlite"))) == 0
    assert _query_detail(_args(conversation_id="c1", exchanges=1, tools="shell", db=str(tmp_path / "db.sqlite"))) == 0


def test_query_sql_and_cmd_query_list_branches(monkeypatch, capsys, tmp_path):
    # _query_sql list and parse failures
    monkeypatch.setattr("siftd.api.list_query_files", lambda: [])
    assert _query_sql(_args(sql_name=None)) == 0

    qf = [SimpleNamespace(name="cost", variables=["ws"])]
    monkeypatch.setattr("siftd.api.list_query_files", lambda: qf)
    assert _query_sql(_args(sql_name=None)) == 0

    assert _query_sql(_args(sql_name="cost", var=["bad"])) == 1

    # query not found + db missing + missing vars + generic query error
    monkeypatch.setattr("siftd.api.run_query_file", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("Query file not found: x")))
    assert _query_sql(_args(sql_name="cost")) == 1

    monkeypatch.setattr("siftd.api.run_query_file", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("missing db")))
    assert _query_sql(_args(sql_name="cost")) == 1

    class _QErr(Exception):
        pass

    monkeypatch.setattr("siftd.api.QueryError", _QErr)
    monkeypatch.setattr("siftd.api.run_query_file", lambda *a, **k: (_ for _ in ()).throw(_QErr("Missing variables: ws")))
    assert _query_sql(_args(sql_name="cost")) == 1

    monkeypatch.setattr("siftd.api.run_query_file", lambda *a, **k: (_ for _ in ()).throw(_QErr("boom")))
    assert _query_sql(_args(sql_name="cost")) == 1

    monkeypatch.setattr("siftd.api.run_query_file", lambda *a, **k: SimpleNamespace(rows=[], columns=[]))
    assert _query_sql(_args(sql_name="cost")) == 0

    monkeypatch.setattr("siftd.api.run_query_file", lambda *a, **k: SimpleNamespace(rows=[[1, None]], columns=["a", "b"]))
    printed = []
    monkeypatch.setattr("siftd.cli.query.print_table", lambda cols, rows: printed.append((cols, rows)))
    assert _query_sql(_args(sql_name="cost")) == 0
    assert printed

    # cmd_query: sql dispatch, detail dispatch, list paths
    monkeypatch.setattr("siftd.cli.query._query_sql", lambda a: 7)
    assert cmd_query(_args(conversation_id="sql")) == 7

    monkeypatch.setattr("siftd.cli.query._query_detail", lambda a: 8)
    assert cmd_query(_args(conversation_id="c1")) == 8

    # list: serve dict deserialization + stats
    monkeypatch.setattr(
        "siftd.serve.delegation.try_serve",
        lambda op: {"conversations": [{"id": "c1", "workspace": "/w", "model": "m", "started_at": "2024", "prompts": 1, "responses": 1, "tokens": 5, "cost": None, "tags": []}]},
    )
    monkeypatch.setattr(
        "siftd.output.format_registry.select_format",
        lambda **k: SimpleNamespace(render_list=lambda convs, fidelity, **ctx: "LIST"),
    )
    monkeypatch.setattr("siftd.output.painted_bridge.emit_output", lambda out: None)
    monkeypatch.setattr(
        "siftd.api.stats.get_usage_summary",
        lambda db_path=None: SimpleNamespace(total_conversations=500, total_input_tokens=1000, total_output_tokens=500, total_cost=0.0),
    )
    assert cmd_query(_args(stats=True, db=str(tmp_path / "db.sqlite"))) == 0

    class _F:
        # Depth=0 keeps caveat producers' applies_to predicates False so
        # they don't run against the SimpleNamespace fixtures below.
        depth = 0

        def with_depth(self, d):
            return self

    monkeypatch.setattr("siftd.cli.query.fidelity_from_args", lambda args: _F())
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: [SimpleNamespace(prompt_count=1, response_count=1, total_tokens=3)])
    assert cmd_query(_args(verbose=True, db=str(tmp_path / "db.sqlite"))) == 0

    # local execution errors
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (_ for _ in ()).throw(FileNotFoundError("missing")))
    assert cmd_query(_args(db=str(tmp_path / "db.sqlite"))) == 1

    import sqlite3

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (_ for _ in ()).throw(sqlite3.OperationalError("no such table: content_fts")))
    assert cmd_query(_args(db=str(tmp_path / "db.sqlite"))) == 1

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (_ for _ in ()).throw(sqlite3.OperationalError("fts5 syntax")))
    assert cmd_query(_args(db=str(tmp_path / "db.sqlite"))) == 1

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (_ for _ in ()).throw(sqlite3.OperationalError("other")))
    assert cmd_query(_args(db=str(tmp_path / "db.sqlite"))) == 1

    # no conversations + hint branches
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: [])
    assert cmd_query(_args(json=True, db=str(tmp_path / "db.sqlite"))) == 0
    assert cmd_query(_args(workspace="/w", db=str(tmp_path / "db.sqlite"))) == 0
    assert cmd_query(_args(tool="bash", db=str(tmp_path / "db.sqlite"))) == 0
    assert cmd_query(_args(db=str(tmp_path / "db.sqlite"))) == 0


def test_stats_corpus_aware(monkeypatch, capsys, tmp_path):
    """--stats line shows view count / corpus total and token comparison."""
    view_convs = [
        SimpleNamespace(prompt_count=1, response_count=1, total_tokens=500),
        SimpleNamespace(prompt_count=2, response_count=2, total_tokens=1000),
    ]
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: view_convs)
    monkeypatch.setattr(
        "siftd.output.format_registry.select_format",
        lambda **k: SimpleNamespace(render_list=lambda convs, fidelity, **ctx: ""),
    )
    monkeypatch.setattr("siftd.output.painted_bridge.emit_output", lambda out: None)
    monkeypatch.setattr(
        "siftd.api.stats.get_usage_summary",
        lambda db_path=None: SimpleNamespace(
            total_conversations=12438,
            total_input_tokens=100_000_000,
            total_output_tokens=42_000_000,
            total_cost=0.0,
        ),
    )

    class _F:
        depth = 0

        def with_depth(self, d):
            return self

    monkeypatch.setattr("siftd.cli.query.fidelity_from_args", lambda args: _F())

    rc = cmd_query(_args(stats=True, db=str(tmp_path / "db.sqlite")))
    assert rc == 0

    out = capsys.readouterr().out
    # view count / corpus count
    assert "View: 2 / 12,438 corpus" in out
    # token totals present (exact formatting via fmt_tokens)
    assert "corpus" in out
    assert "view tokens:" in out


def test_stats_corpus_aware_workspace_filter(monkeypatch, capsys, tmp_path):
    """--stats with -w shows filtered view count against unfiltered corpus."""
    view_convs = [SimpleNamespace(prompt_count=1, response_count=1, total_tokens=500)]
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: view_convs)
    monkeypatch.setattr(
        "siftd.output.format_registry.select_format",
        lambda **k: SimpleNamespace(render_list=lambda convs, fidelity, **ctx: ""),
    )
    monkeypatch.setattr("siftd.output.painted_bridge.emit_output", lambda out: None)
    monkeypatch.setattr(
        "siftd.api.stats.get_usage_summary",
        lambda db_path=None: SimpleNamespace(
            total_conversations=12438,
            total_input_tokens=100_000_000,
            total_output_tokens=42_000_000,
            total_cost=0.0,
        ),
    )

    class _F:
        depth = 0

        def with_depth(self, d):
            return self

    monkeypatch.setattr("siftd.cli.query.fidelity_from_args", lambda args: _F())

    rc = cmd_query(_args(stats=True, workspace="/w", db=str(tmp_path / "db.sqlite")))
    assert rc == 0

    out = capsys.readouterr().out
    assert "View: 1 / 12,438 corpus" in out
    assert "view tokens:" in out


def test_stats_corpus_aware_empty_view_shows_stats(monkeypatch, capsys, tmp_path):
    """--stats still prints view/corpus comparison when filtered view is empty."""
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: [])
    monkeypatch.setattr(
        "siftd.api.stats.get_usage_summary",
        lambda db_path=None: SimpleNamespace(
            total_conversations=12438,
            total_input_tokens=100_000_000,
            total_output_tokens=42_000_000,
            total_cost=0.0,
        ),
    )

    rc = cmd_query(_args(stats=True, workspace="/w", db=str(tmp_path / "db.sqlite")))
    assert rc == 0

    out = capsys.readouterr().out
    assert "No conversations found." in out
    assert "View: 0 / 12,438 corpus" in out
    assert "view tokens: 0 /" in out


def test_no_hints_suppresses_hint_caveats(monkeypatch, capsys, tmp_path):
    """--no-hints drops severity="hint" caveats before render."""
    from siftd.doctor.checks import Finding

    hint_finding = Finding(check="c", severity="hint", message="hint-msg", fix_available=False)
    info_finding = Finding(check="d", severity="info", message="info-msg", fix_available=False)

    class _F:
        depth = 0

        def with_depth(self, d):
            return self

    monkeypatch.setattr("siftd.cli.query.fidelity_from_args", lambda args: _F())
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)

    from types import SimpleNamespace

    monkeypatch.setattr(
        "siftd.api.dispatch.execute_for_render",
        lambda op: (
            [SimpleNamespace(prompt_count=1, response_count=1, total_tokens=3)],
            [hint_finding, info_finding],
        ),
    )

    captured_caveats = []

    def _mock_render_list(convs, fidelity, **ctx):
        captured_caveats.extend(ctx.get("caveats", []))
        return ""

    monkeypatch.setattr(
        "siftd.output.format_registry.select_format",
        lambda **k: SimpleNamespace(render_list=_mock_render_list),
    )
    monkeypatch.setattr("siftd.output.painted_bridge.emit_output", lambda out: None)

    rc = cmd_query(_args(no_hints=True, db=str(tmp_path / "db.sqlite")))
    assert rc == 0
    assert all(f.severity != "hint" for f in captured_caveats)
    assert any(f.severity == "info" for f in captured_caveats)
