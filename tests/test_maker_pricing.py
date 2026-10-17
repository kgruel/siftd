"""A response is priced at its serving provider's rate, else at its model maker's.

siftd's cost is the model maker's list price for the tokens. A harness whose source
is ``multi`` (pi, copilot_cli, opencode, vscode, aider) records ``multi`` — or a
router such as ``github-copilot`` — as the serving provider, and no price is filed
under either; the price is filed under the maker (``anthropic``, ``openai``). These
drive the real pi_agent and copilot_cli parsers through the real ingest, then read
what the rollup, the unpriced report and a slice say about the result.

Prices come from a user override file, so the numbers here do not move when the
shipped reference is re-synced.
"""

import json
import sqlite3
from pathlib import Path

import pytest

import siftd.pricing as pricing_mod
from siftd.adapters import copilot_cli, pi_agent
from siftd.api.slice import slice_database
from siftd.ingestion import ingest_all
from siftd.storage.sqlite import (
    SCHEMA_VERSION,
    create_database,
    get_models_without_pricing,
    open_database,
)
from siftd.storage.usage_rollup import rebuild_rollups

FIXTURES = Path(__file__).parent / "fixtures" / "schemas"
COPILOT_FIXTURE = Path(__file__).parent / "fixtures" / "adapters" / "copilot_cli" / "minimal" / "input.jsonl"

MTOK = 1_000_000

PRICES = """
[[price]]
model = "claude-haiku-4-5"
provider = "anthropic"
input_per_mtok = 1.0
output_per_mtok = 5.0
source = "test"
as_of = "2026-10-16"

[[price]]
model = "gpt-4o"
provider = "openai"
input_per_mtok = 2.5
output_per_mtok = 10.0
source = "test"
as_of = "2026-10-16"
"""

ROUTER_PRICE = """
[[price]]
model = "gpt-4o"
provider = "github-copilot"
input_per_mtok = 1.0
output_per_mtok = 2.0
source = "test"
as_of = "2026-10-16"
"""


@pytest.fixture
def prices(tmp_path, monkeypatch):
    """Install the override price file; returns a function that appends entries."""
    override = tmp_path / "pricing.toml"
    override.write_text(PRICES)
    monkeypatch.setattr(pricing_mod, "pricing_override_file", lambda: override)

    def add(toml: str) -> None:
        override.write_text(PRICES + toml)

    return add


def _assistant(ts: str, provider: str | None, model: str, inp: int, out: int) -> dict:
    message = {
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": f"reply from {model}"}],
        "usage": {"input": inp, "output": out, "cacheRead": 0, "cacheWrite": 0,
                  "totalTokens": inp + out},
    }
    if provider is not None:
        message["provider"] = provider
    return {"type": "message", "id": f"a-{ts}", "timestamp": ts, "message": message}


def _pi_session(root: Path, responses: list[tuple[str | None, str, int, int]]) -> None:
    """One pi session: a prompt, then one assistant message per response."""
    records = [
        {"type": "session", "version": 3, "id": "pi-maker", "timestamp": "2026-10-01T10:00:00Z",
         "cwd": "/test/workspace"},
        {"type": "message", "id": "u-1", "timestamp": "2026-10-01T10:00:01Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
    ]
    for i, (provider, model, inp, out) in enumerate(responses):
        records.append(_assistant(f"2026-10-01T10:00:{i + 2:02d}Z", provider, model, inp, out))
    path = root / "session.jsonl"
    root.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _ingest_pi(tmp_path, monkeypatch, responses):
    """Ingest a pi session, then reopen: the pricing projection runs on open."""
    root = tmp_path / ".pi" / "agent" / "sessions"
    _pi_session(root, responses)
    monkeypatch.setattr(pi_agent, "DEFAULT_LOCATIONS", [str(root)])
    db = tmp_path / "siftd.db"
    conn = create_database(db)
    ingest_all(conn, [pi_agent], filter_binary=False)
    conn.close()
    return _reprice(db)


def _reprice(db: Path) -> sqlite3.Connection:
    """The state after a later open + rebuild: prices projected onto the models ingest created."""
    conn = open_database(db)
    rebuild_rollups(conn, commit=True)
    return conn


def _costs(conn) -> dict[tuple[str, str], float | None]:
    """(model name, serving provider) -> rollup cost."""
    rows = conn.execute(
        "SELECT m.name AS model, p.name AS provider, u.cost AS cost "
        "FROM usage_by_conv_model u JOIN models m ON m.id = u.model_id "
        "LEFT JOIN providers p ON p.id = u.provider_id"
    ).fetchall()
    return {(r["model"], r["provider"]): r["cost"] for r in rows}


class TestMakerFallback:
    def test_a_multi_response_with_no_provider_is_priced_at_the_makers_rate(self, tmp_path, monkeypatch, prices):
        """copilot_cli's shape: provider unnamed, harness source 'multi', no price under 'multi'."""
        conn = _ingest_pi(tmp_path, monkeypatch, [(None, "claude-haiku-4-5", MTOK, MTOK)])
        costs = _costs(conn)
        assert list(costs) == [("claude-haiku-4-5", "multi")]
        assert costs[("claude-haiku-4-5", "multi")] == pytest.approx(1.0 + 5.0)  # anthropic's rate
        assert get_models_without_pricing(conn) == []

    def test_a_router_response_is_priced_at_the_makers_rate(self, tmp_path, monkeypatch, prices):
        conn = _ingest_pi(tmp_path, monkeypatch, [("github-copilot", "gpt-4o", MTOK, MTOK)])
        costs = _costs(conn)
        assert costs[("gpt-4o", "github-copilot")] == pytest.approx(2.5 + 10.0)  # openai's rate
        assert get_models_without_pricing(conn) == []

    def test_a_price_filed_for_the_serving_provider_beats_the_makers(self, tmp_path, monkeypatch, prices):
        prices(ROUTER_PRICE)
        conn = _ingest_pi(tmp_path, monkeypatch, [
            ("github-copilot", "gpt-4o", MTOK, MTOK),  # the router's own rate
            ("openai-codex", "gpt-4o", MTOK, MTOK),    # served by openai itself
        ])
        costs = _costs(conn)
        assert costs[("gpt-4o", "github-copilot")] == pytest.approx(1.0 + 2.0)
        assert costs[("gpt-4o", "openai")] == pytest.approx(2.5 + 10.0)

    def test_a_serving_row_with_no_input_rate_does_not_shadow_the_makers(self, tmp_path, monkeypatch, prices):
        conn = _ingest_pi(tmp_path, monkeypatch, [("github-copilot", "gpt-4o", MTOK, MTOK)])
        conn.execute(
            "UPDATE pricing SET input_per_mtok = NULL "
            "WHERE provider_id = (SELECT id FROM providers WHERE name = 'github-copilot')"
        )
        conn.execute(
            "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok) "
            "SELECT 'x', m.id, p.id, NULL, 99 FROM models m, providers p "
            "WHERE m.name = 'gpt-4o' AND p.name = 'github-copilot'"
        )
        conn.commit()
        rebuild_rollups(conn, commit=True)
        assert _costs(conn)[("gpt-4o", "github-copilot")] == pytest.approx(2.5 + 10.0)

    def test_a_local_model_stays_unpriced_and_is_reported(self, tmp_path, monkeypatch, prices):
        conn = _ingest_pi(tmp_path, monkeypatch, [
            ("ollama", "gemma4:e4b", MTOK, MTOK),
            ("github-copilot", "gpt-4o", MTOK, MTOK),
        ])
        costs = _costs(conn)
        assert costs[("gemma4:e4b", "ollama")] is None  # NULL, never $0
        assert costs[("gpt-4o", "github-copilot")] is not None
        assert get_models_without_pricing(conn) == [
            {"model_name": "gemma4:e4b", "provider_name": "ollama"}
        ]

    def test_a_maker_with_no_serving_use_still_gets_its_prices_projected(self, tmp_path, monkeypatch, prices):
        """A pi-only database never sees 'anthropic' serve; its price must still project."""
        conn = _ingest_pi(tmp_path, monkeypatch, [(None, "claude-haiku-4-5", MTOK, MTOK)])
        assert conn.execute("SELECT 1 FROM providers WHERE name = 'anthropic'").fetchone()
        assert conn.execute(
            "SELECT 1 FROM event_response er JOIN providers p ON p.id = er.provider_id "
            "WHERE p.name = 'anthropic'"
        ).fetchone() is None  # nothing was served by anthropic: the row is for the fallback only

    def test_the_real_copilot_cli_fixture_is_priced_through_its_maker(self, tmp_path, monkeypatch, prices):
        """The shipped copilot_cli shape end to end: model 'claude-haiku-4.5', source 'multi'.

        copilot responses carry no usage, so the price shows as a cost of 0 tokens
        (priced, $0.00) rather than NULL (unpriced): the response is covered.
        """
        root = tmp_path / ".copilot" / "session-state" / "copilot-session-001"
        root.mkdir(parents=True)
        (root / "events.jsonl").write_text(COPILOT_FIXTURE.read_text())
        monkeypatch.setattr(copilot_cli, "DEFAULT_LOCATIONS", [str(tmp_path / ".copilot" / "session-state")])
        db = tmp_path / "siftd.db"
        conn = create_database(db)
        ingest_all(conn, [copilot_cli], filter_binary=False)
        conn.close()
        conn = _reprice(db)
        assert _costs(conn) == {("claude-haiku-4-5", "multi"): 0.0}
        assert get_models_without_pricing(conn) == []


class TestShippedReport:
    def test_the_cost_report_agrees_with_the_rollup_for_maker_priced_and_unpriced_rows(
        self, tmp_path, monkeypatch, prices
    ):
        from importlib.resources import files

        conn = _ingest_pi(tmp_path, monkeypatch, [
            (None, "claude-haiku-4-5", MTOK, MTOK),
            ("github-copilot", "gpt-4o", MTOK, MTOK),
            ("ollama", "gemma4:e4b", MTOK, MTOK),
        ])
        sql = files("siftd.builtin_queries").joinpath("cost.sql").read_text().replace("$limit", "10")
        report = {(r["model"], r["provider"]): r["approx_cost_usd"] for r in conn.execute(sql)}
        assert report == {
            ("claude-haiku-4-5", "multi"): pytest.approx(6.0),
            ("gpt-4o", "github-copilot"): pytest.approx(12.5),
            ("gemma4:e4b", "ollama"): None,  # unpriced is not $0
        }


class TestSlice:
    def test_a_slice_carries_the_makers_price_row(self, tmp_path, monkeypatch, prices):
        conn = _ingest_pi(tmp_path, monkeypatch, [
            (None, "claude-haiku-4-5", MTOK, MTOK),
            ("github-copilot", "gpt-4o", MTOK, MTOK),
        ])
        source_costs = _costs(conn)
        conn.close()

        target = tmp_path / "slice.db"
        slice_database(tmp_path / "siftd.db", target)

        sliced = sqlite3.connect(target)
        sliced.row_factory = sqlite3.Row
        try:
            maker_rows = {
                (r["model"], r["provider"])
                for r in sliced.execute(
                    "SELECT m.name AS model, p.name AS provider FROM pricing pr "
                    "JOIN models m ON m.id = pr.model_id JOIN providers p ON p.id = pr.provider_id"
                )
            }
            assert ("claude-haiku-4-5", "anthropic") in maker_rows
            assert ("gpt-4o", "openai") in maker_rows
            sliced_costs = _costs(sliced)
        finally:
            sliced.close()
        # The slice reprices every response exactly as the source did.
        assert sliced_costs == pytest.approx(source_costs)
        assert all(cost is not None for cost in sliced_costs.values())


    def test_a_maker_price_the_reference_lacks_travels_with_the_slice(self, tmp_path, monkeypatch, prices):
        """The target reprojects the reference on open, so only a price filed by hand needs the copy."""
        conn = _ingest_pi(tmp_path, monkeypatch, [("github-copilot", "gpt-4o-mini", MTOK, MTOK)])
        conn.execute("INSERT INTO providers (id, name) VALUES ('p-openai', 'openai')")
        conn.execute(
            "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok) "
            "SELECT 'pr-hand', m.id, 'p-openai', 0.5, 1.5 FROM models m WHERE m.name = 'gpt-4o-mini'"
        )
        conn.commit()
        rebuild_rollups(conn, commit=True)
        assert _costs(conn)[("gpt-4o-mini", "github-copilot")] == pytest.approx(2.0)
        conn.close()

        target = tmp_path / "slice.db"
        slice_database(tmp_path / "siftd.db", target)

        sliced = sqlite3.connect(target)
        sliced.row_factory = sqlite3.Row
        try:
            assert _costs(sliced) == {("gpt-4o-mini", "github-copilot"): pytest.approx(2.0)}
        finally:
            sliced.close()


class TestMigration:
    def test_v14_gives_an_existing_database_its_makers_and_reprices(self, tmp_path, prices):
        """A v13 database whose models predate the creator parse: the open upgrades it."""
        db = tmp_path / "v13.db"
        raw = sqlite3.connect(db)
        raw.executescript((FIXTURES / "v13.sql").read_text())
        raw.executescript(
            """
            INSERT INTO harnesses (id, name, source, log_format) VALUES ('h', 'pi_agent', 'multi', 'jsonl');
            INSERT INTO workspaces (id, path, discovered_at) VALUES ('w', '/p', '2026-10-01');
            INSERT INTO providers (id, name) VALUES ('pm', 'multi');
            INSERT INTO models (id, raw_name, name) VALUES ('m4o', 'gpt-4o', 'gpt-4o');
            INSERT INTO models (id, raw_name, name) VALUES ('mgem', 'gemma4:e4b', 'gemma4:e4b');
            INSERT INTO conversations (id, external_id, harness_id, workspace_id, started_at)
                VALUES ('c', 'pi::1', 'h', 'w', '2026-10-01');
            INSERT INTO events (id, kind, conversation_id, timestamp) VALUES ('e1', 'response', 'c', '2026-10-01');
            INSERT INTO event_response (event_id, model_id, provider_id, input_tokens, output_tokens)
                VALUES ('e1', 'm4o', 'pm', 1000000, 1000000);
            INSERT INTO events (id, kind, conversation_id, timestamp) VALUES ('e2', 'response', 'c', '2026-10-01');
            INSERT INTO event_response (event_id, model_id, provider_id, input_tokens, output_tokens)
                VALUES ('e2', 'mgem', 'pm', 1000000, 1000000);
            """
        )
        raw.commit()
        raw.close()

        conn = open_database(db)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            creators = dict(conn.execute("SELECT raw_name, creator FROM models").fetchall())
            assert creators == {"gpt-4o": "openai", "gemma4:e4b": None}
            assert _costs(conn) == {
                ("gpt-4o", "multi"): pytest.approx(2.5 + 10.0),
                ("gemma4:e4b", "multi"): None,
            }
        finally:
            conn.close()
