"""Database statistics API."""

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from siftd.paths import cache_dir
from siftd.paths import db_path as default_db_path
from siftd.storage.conversation_stats import CostCoverage
from siftd.storage.queries import (
    fetch_conversation_time_window,
    fetch_harness_conversation_counts,
    fetch_harnesses,
    fetch_last_ingest_time,
    fetch_model_names,
    fetch_response_token_coverage,
    fetch_table_count,
    fetch_token_coverage_by_harness,
    fetch_top_conversation_tags,
    fetch_top_tools,
    fetch_top_workspaces,
)
from siftd.storage.sql_helpers import has_conversation_owners_table
from siftd.storage.sqlite import open_database


@dataclass
class TableCounts:
    """Row counts for core tables."""

    conversations: int
    prompts: int
    responses: int
    tool_calls: int
    harnesses: int
    workspaces: int
    tools: int
    models: int
    ingested_files: int


@dataclass
class HarnessInfo:
    """Harness metadata."""

    name: str
    source: str | None
    log_format: str | None


@dataclass
class WorkspaceStats:
    """Workspace with conversation count."""

    path: str
    conversation_count: int
    last_activity: str | None


@dataclass
class ToolStats:
    """Tool with usage count."""

    name: str
    usage_count: int


@dataclass
class HarnessCount:
    """Conversation count by harness."""

    name: str
    conversation_count: int


@dataclass
class TagStats:
    """Tag usage count."""

    name: str
    count: int


@dataclass
class TokenCoverageByHarness:
    """Token coverage summary for a harness."""

    name: str
    responses: int
    with_tokens: int
    pct_with_tokens: float


@dataclass
class TokenCoverage:
    """Overall token coverage summary."""

    responses: int
    with_tokens: int
    pct_with_tokens: float
    by_harness: list[TokenCoverageByHarness]


@dataclass
class DatabaseStats:
    """Complete database statistics."""

    db_path: Path
    db_size_bytes: int
    counts: TableCounts
    harnesses: list[HarnessInfo]
    harness_counts: list[HarnessCount]
    top_workspaces: list[WorkspaceStats]
    models: list[str]
    top_tools: list[ToolStats]
    top_tags: list[TagStats]
    token_coverage: TokenCoverage
    activity_window: tuple[str | None, str | None]
    last_ingest_at: str | None


def get_cost_coverage(
    conn: sqlite3.Connection | None = None,
    *,
    db_path: Path | None = None,
    owner: str | None = None,
) -> CostCoverage | None:
    """Get cost coverage statistics from conversation_stats.

    Returns None if the conversation_stats table does not exist.

    Cost coverage is measured as the fraction of token-bearing conversations
    that have a positive computed cost (cost > 0).  Conversations with NULL cost
    have no pricing data available; conversations with cost = 0.0 have tokens
    but were priced at zero (indicates stale stats — run siftd ingest to rebuild).

    When ``owner`` is set, scoped to conversations owned by that user_id;
    ``owner=None`` is unscoped, the single-tenant/local default.

    Thin wrapper: opens a read-only connection when ``conn`` is not supplied and
    delegates to :func:`siftd.storage.conversation_stats.get_cost_coverage`, the
    single definition of the coverage FILTER.
    """
    from siftd.storage.conversation_stats import (
        get_cost_coverage as _storage_get_cost_coverage,
    )
    from siftd.storage.sqlite import open_database

    should_close = False
    if conn is None:
        path = db_path or default_db_path()
        conn = open_database(path, read_only=True)
        should_close = True

    try:
        return _storage_get_cost_coverage(conn, owner=owner)
    finally:
        if should_close:
            conn.close()


def list_models(
    conn: sqlite3.Connection | None = None,
    *,
    db_path: Path | None = None,
    owner: str | None = None,
) -> list[str]:
    """List canonical model names, optionally scoped to an owner.

    A cheap projection for filter UIs — ``get_stats`` returns the same list but
    pays for full-table counts and token coverage to get it.

    Args:
        conn: Database connection. Opened from db_path if not provided.
        db_path: Path to database. Ignored if conn provided.
        owner: Scope to conversations owned by this identity.

    Returns:
        Sorted, deduped canonical model names.
    """
    should_close = False
    if conn is None:
        db = db_path or default_db_path()
        conn = open_database(db, read_only=True)
        should_close = True
    try:
        owner_kw = {"owner": owner} if owner else {}
        return fetch_model_names(conn, **owner_kw)
    finally:
        if should_close:
            conn.close()


def list_workspaces(
    conn: sqlite3.Connection | None = None,
    n: int = 10,
    *,
    db_path: Path | None = None,
    owner: str | None = None,
    with_usage: bool = False,
    sort: str = "sessions",
) -> list[sqlite3.Row]:
    """List workspaces with conversation counts.

    Args:
        conn: Database connection. Opened from db_path if not provided.
        n: Maximum workspaces to return.
        db_path: Path to database. Ignored if conn provided.
        with_usage: Also return ``inp``/``out``/``cost`` columns from the rollup
            (cost ``None`` when the workspace has no priced usage). Off by default
            so the name-only callers stay on the lean query; the Workspaces view
            opts in.
        sort: Ordering — ``sessions`` (default), ``recent``, ``tokens``, or
            ``cost``. ``tokens``/``cost`` require ``with_usage`` and fall back to
            ``sessions`` otherwise.

    Returns:
        Rows with 'id' (workspace ULID), 'path', 'git_remote', 'convs',
        'last_activity', and 'pinned' (0/1, owner-scoped) keys (plus
        'inp'/'out'/'cost' when ``with_usage``). The ULID 'id' is the workspace's
        stable identity (workspaces.id) — the read API addresses workspaces by
        it, not by the slash-containing path.
    """
    should_close = False
    if conn is None:
        db = db_path or default_db_path()
        conn = open_database(db, read_only=True)
        should_close = True
    try:
        owner_kw = {"owner": owner} if owner else {}
        return fetch_top_workspaces(
            conn, limit=n, with_usage=with_usage, sort=sort, **owner_kw
        )
    finally:
        if should_close:
            conn.close()


def set_workspace_pin(
    workspace_id: str,
    *,
    pinned: bool,
    db_path: Path | None = None,
    owner: str | None = None,
) -> bool:
    """Pin or unpin a workspace (by ULID) for an owner. Returns True if state changed.

    Mirrors :func:`siftd.api.tags.set_tag_pin`, where the workspace analogue of
    "the tag exists" is "the owner participates": a *pin* requires the workspace
    to exist and — under an owner scope — that the owner has a conversation there,
    so you can only pin what your owner-scoped list can show (no stranded pin, no
    surfacing a foreign workspace later). The local/unscoped case (``owner`` None)
    keeps the existence-only guard, matching its see-everything view. *Unpin* is
    always allowed (it only removes state), so a pin can never become unreachable.
    Owner-scoped, so one tenant's pins never touch another's view. Manages its own
    connection and transaction.
    """
    from siftd.storage.queries import owner_participates_in_workspace as _participates
    from siftd.storage.queries import pin_workspace as _pin
    from siftd.storage.queries import unpin_workspace as _unpin
    from siftd.storage.queries import workspace_exists as _exists

    wid = (workspace_id or "").strip()
    if not wid:
        return False

    path = db_path or default_db_path()
    conn = open_database(path)
    try:
        if not pinned:
            # Unpin always allowed: it only removes the owner's own state, and
            # gating it could strand a pin whose workspace dropped from the list.
            changed = _unpin(conn, owner=owner, workspace_id=wid)
        elif not _exists(conn, wid):
            return False
        elif owner and not _participates(conn, owner, wid):
            return False  # can't pin a workspace this owner can't see
        else:
            changed = _pin(conn, owner=owner, workspace_id=wid)
        conn.commit()
        return changed
    finally:
        conn.close()


def stats_cache_path(owner: str | None = None) -> Path:
    """Return path to the stats cache file.

    The cache is owner-dimensional: ingest writes the unscoped (owner=None)
    file; owner-scoped consumers (the serve dashboard) get their own file so
    a tenant never reads cross-tenant totals. Owner is hashed into the name —
    it is an identity string (email/sub), not filesystem-safe.
    """
    if owner is None:
        return cache_dir() / "stats.json"
    import hashlib

    digest = hashlib.sha256(owner.encode()).hexdigest()[:16]
    return cache_dir() / f"stats.{digest}.json"


def _stats_to_dict(stats: DatabaseStats) -> dict:
    """Serialize DatabaseStats to a JSON-safe dict."""
    return {
        "db_path": str(stats.db_path),
        "db_size_bytes": stats.db_size_bytes,
        "counts": {
            "conversations": stats.counts.conversations,
            "prompts": stats.counts.prompts,
            "responses": stats.counts.responses,
            "tool_calls": stats.counts.tool_calls,
            "harnesses": stats.counts.harnesses,
            "workspaces": stats.counts.workspaces,
            "tools": stats.counts.tools,
            "models": stats.counts.models,
            "ingested_files": stats.counts.ingested_files,
        },
        "harnesses": [
            {"name": h.name, "source": h.source, "log_format": h.log_format}
            for h in stats.harnesses
        ],
        "harness_counts": [
            {"name": hc.name, "conversation_count": hc.conversation_count}
            for hc in stats.harness_counts
        ],
        "top_workspaces": [
            {
                "path": w.path,
                "conversation_count": w.conversation_count,
                "last_activity": w.last_activity,
            }
            for w in stats.top_workspaces
        ],
        "models": stats.models,
        "top_tools": [
            {"name": t.name, "usage_count": t.usage_count}
            for t in stats.top_tools
        ],
        "top_tags": [
            {"name": t.name, "count": t.count} for t in stats.top_tags
        ],
        "token_coverage": {
            "responses": stats.token_coverage.responses,
            "with_tokens": stats.token_coverage.with_tokens,
            "pct_with_tokens": stats.token_coverage.pct_with_tokens,
            "by_harness": [
                {
                    "name": h.name,
                    "responses": h.responses,
                    "with_tokens": h.with_tokens,
                    "pct_with_tokens": h.pct_with_tokens,
                }
                for h in stats.token_coverage.by_harness
            ],
        },
        "activity_window": list(stats.activity_window),
        "last_ingest_at": stats.last_ingest_at,
    }



def dict_to_stats(data: dict) -> DatabaseStats:
    """Deserialize a JSON dict back to DatabaseStats."""
    c = data["counts"]
    tc = data["token_coverage"]
    aw = data["activity_window"]

    return DatabaseStats(
        db_path=Path(data["db_path"]),
        db_size_bytes=data["db_size_bytes"],
        counts=TableCounts(**c),
        harnesses=[
            HarnessInfo(name=h["name"], source=h.get("source"), log_format=h.get("log_format"))
            for h in data["harnesses"]
        ],
        harness_counts=[
            HarnessCount(name=hc["name"], conversation_count=hc["conversation_count"])
            for hc in data["harness_counts"]
        ],
        top_workspaces=[
            WorkspaceStats(path=w["path"], conversation_count=w["conversation_count"], last_activity=w.get("last_activity"))
            for w in data["top_workspaces"]
        ],
        models=data["models"],
        top_tools=[ToolStats(name=t["name"], usage_count=t["usage_count"]) for t in data["top_tools"]],
        top_tags=[TagStats(name=t["name"], count=t["count"]) for t in data["top_tags"]],
        token_coverage=TokenCoverage(
            responses=tc["responses"],
            with_tokens=tc["with_tokens"],
            pct_with_tokens=tc["pct_with_tokens"],
            by_harness=[
                TokenCoverageByHarness(name=h["name"], responses=h["responses"], with_tokens=h["with_tokens"], pct_with_tokens=h["pct_with_tokens"])
                for h in tc["by_harness"]
            ],
        ),
        activity_window=(aw[0], aw[1]),
        last_ingest_at=data.get("last_ingest_at"),
    )


def _dict_to_stats(data: dict) -> DatabaseStats:
    """Backward-compatible alias for dict_to_stats()."""
    return dict_to_stats(data)


def write_stats_cache(stats: DatabaseStats, *, owner: str | None = None) -> None:
    """Atomically write stats to the cache file (per-owner when scoped).

    Includes db_mtime_ns for staleness detection and computed_at timestamp.
    """
    payload = {
        "_meta": {
            "computed_at": datetime.now(UTC).isoformat(),
            "db_mtime_ns": stats.db_path.stat().st_mtime_ns if stats.db_path.exists() else 0,
        },
        **_stats_to_dict(stats),
    }
    dest = stats_cache_path(owner)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_stats_cache(
    *,
    db_path: Path | None = None,
    owner: str | None = None,
    require_fresh: bool = False,
) -> DatabaseStats | None:
    """Read cached stats if the cache exists and matches.

    Returns None if cache is missing, corrupt, or the db_path doesn't match.
    With require_fresh, also returns None when the DB file changed since the
    cache was computed (db_mtime_ns mismatch) — the CLI's tiered fallback
    prefers a possibly-stale answer over a cold recompute, but the serve
    dashboard must reflect a push-ingest, so it opts in.
    """
    path = stats_cache_path(owner)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    # Verify db_path matches (prevents stale cache from a different DB)
    effective_db = db_path or default_db_path()
    cached_db = Path(data.get("db_path", ""))
    if cached_db.resolve() != effective_db.resolve():
        return None

    if require_fresh:
        try:
            current_mtime = effective_db.stat().st_mtime_ns
        except OSError:
            return None
        if data.get("_meta", {}).get("db_mtime_ns") != current_mtime:
            return None

    return dict_to_stats(data)


def get_stats(*, db_path: Path | None = None, owner: str | None = None) -> DatabaseStats:
    """Get comprehensive database statistics.

    Args:
        db_path: Path to database. Uses default if not specified.

    Returns:
        DatabaseStats with counts, harnesses, workspaces, models, tools.

    Raises:
        FileNotFoundError: If database does not exist.
    """
    db = db_path or default_db_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    conn = open_database(db, read_only=True)
    try:
        if owner and not has_conversation_owners_table(conn):
            empty_counts = TableCounts(
                conversations=0, prompts=0, responses=0, tool_calls=0,
                harnesses=0, workspaces=0, tools=0, models=0, ingested_files=0,
            )
            empty_token = TokenCoverage(responses=0, with_tokens=0, pct_with_tokens=0.0, by_harness=[])
            return DatabaseStats(
                db_path=db,
                db_size_bytes=db.stat().st_size,
                counts=empty_counts,
                harnesses=[],
                harness_counts=[],
                top_workspaces=[],
                models=[],
                top_tools=[],
                top_tags=[],
                token_coverage=empty_token,
                activity_window=(None, None),
                last_ingest_at=None,
            )

        owner_kw = {"owner": owner} if owner else {}

        # Table counts
        table_names = [
            "conversations",
            "prompts",
            "responses",
            "tool_calls",
            "harnesses",
            "workspaces",
            "tools",
            "models",
            "ingested_files",
        ]
        count_values = {name: fetch_table_count(conn, name, **owner_kw) for name in table_names}
        counts = TableCounts(**count_values)

        # Harnesses
        harness_rows = fetch_harnesses(conn, **owner_kw)
        harnesses = [
            HarnessInfo(
                name=row["name"],
                source=row["source"],
                log_format=row["log_format"],
            )
            for row in harness_rows
        ]

        # Top workspaces (owner passed explicitly so the bool with_usage kwarg
        # isn't shadowed by the str-typed **owner_kw spread).
        workspace_rows = fetch_top_workspaces(conn, limit=10, owner=owner)
        top_workspaces = [
            WorkspaceStats(
                path=row["path"],
                conversation_count=row["convs"],
                last_activity=row["last_activity"],
            )
            for row in workspace_rows
            if row["path"] is not None
        ]

        # Models
        models = fetch_model_names(conn, **owner_kw)

        # Top tools by usage
        tool_rows = fetch_top_tools(conn, limit=10, **owner_kw)
        top_tools = [
            ToolStats(name=row["name"], usage_count=row["uses"]) for row in tool_rows
        ]

        # Harness conversation counts
        harness_count_rows = fetch_harness_conversation_counts(conn, **owner_kw)
        harness_counts = [
            HarnessCount(name=row["name"], conversation_count=row["conversations"])
            for row in harness_count_rows
        ]

        # Top conversation tags
        tag_rows = fetch_top_conversation_tags(conn, limit=5, **owner_kw)
        top_tags = [TagStats(name=row["name"], count=row["count"]) for row in tag_rows]

        # Token coverage
        total_responses, responses_with_tokens = fetch_response_token_coverage(conn, **owner_kw)
        pct_with_tokens = (
            round((responses_with_tokens / total_responses) * 100, 2)
            if total_responses
            else 0.0
        )
        harness_rows = fetch_token_coverage_by_harness(conn, **owner_kw)
        token_by_harness = []
        for row in harness_rows:
            responses = row["responses"]
            with_tokens = row["with_tokens"] if row["with_tokens"] is not None else 0
            pct = round((with_tokens / responses) * 100, 2) if responses else 0.0
            token_by_harness.append(
                TokenCoverageByHarness(
                    name=row["harness"],
                    responses=responses,
                    with_tokens=with_tokens,
                    pct_with_tokens=pct,
                )
            )

        # Activity window and ingest recency
        activity_window = fetch_conversation_time_window(conn, **owner_kw)
        last_ingest_at = fetch_last_ingest_time(conn, **owner_kw)
    finally:
        conn.close()

    return DatabaseStats(
        db_path=db,
        db_size_bytes=db.stat().st_size,
        counts=counts,
        harnesses=harnesses,
        harness_counts=harness_counts,
        top_workspaces=top_workspaces,
        models=models,
        top_tools=top_tools,
        top_tags=top_tags,
        token_coverage=TokenCoverage(
            responses=total_responses,
            with_tokens=responses_with_tokens,
            pct_with_tokens=pct_with_tokens,
            by_harness=token_by_harness,
        ),
        activity_window=activity_window,
        last_ingest_at=last_ingest_at,
    )



@dataclass
class UsageSummary:
    """Aggregated token/cost stats.

    ``total_input_tokens`` is the TRUE TOTAL input (uncached + cache_read +
    cache_creation — the rollup's normalized convention). The two cache
    components are broken out so the input economy (how much of the input was
    served cheaply from cache vs. paid fresh) is derivable:
    ``uncached = total_input - cache_read - cache_creation``. Defaults keep the
    pre-cache 4-arg constructors valid.
    """

    total_conversations: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost: float
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0


@dataclass
class GroupUsage:
    """Token/cost breakdown for a single group (model or workspace).

    ``cost`` is ``None`` when the group has no priced usage — the same
    NULL-means-unpriced invariant :class:`~siftd.api.conversations.ConversationDetail`
    carries — so consumers render "unknown" rather than a fabricated ``$0`` that
    would re-introduce the mispricing the rollup work removed. A genuine summed
    ``0.0`` (priced rows that net to zero) stays distinct from that ``None``.
    """

    name: str
    conversations: int
    input_tokens: int
    output_tokens: int
    cost: float | None


def get_usage_summary(*, db_path: Path | None = None, owner: str | None = None) -> UsageSummary:
    """Get aggregate token/cost totals across all conversations.

    When ``owner`` is set, totals are scoped to conversations owned by that
    user_id (via :func:`owner_predicate`); ``owner=None`` is unscoped, the
    single-tenant/local default.
    """
    from siftd.storage.sql_helpers import has_conversation_owners_table, owner_predicate
    from siftd.storage.sqlite import open_database

    path = db_path or default_db_path()
    conn = open_database(path, read_only=True)
    try:
        if owner and not has_conversation_owners_table(conn):
            return UsageSummary(0, 0, 0, 0.0)
        conv_where = f" WHERE {owner_predicate('c.id')}" if owner else ""
        conv_params = [owner] if owner else []
        # Conversation count + token totals. The count stays over `conversations`
        # (LEFT JOIN keeps zero-response conversations in the corpus total); the
        # token sums come from the rollup (usage_by_conv_model — the single usage
        # fact) instead of re-descending to event_response.
        row = conn.execute(
            "SELECT COUNT(DISTINCT c.id) AS n,"
            " COALESCE(SUM(u.input_tokens), 0) AS inp,"
            " COALESCE(SUM(u.output_tokens), 0) AS out,"
            " COALESCE(SUM(u.cache_read_tokens), 0) AS cread,"
            " COALESCE(SUM(u.cache_creation_tokens), 0) AS ccreate"
            " FROM conversations c"
            " LEFT JOIN usage_by_conv_model u ON u.conversation_id = c.id"
            f"{conv_where}",
            conv_params,
        ).fetchone()
        # Cost is per-conversation in conversation_stats (the rollup's conv-grain
        # cache, already rounded once per conversation), summed separately.
        has_stats = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='conversation_stats'"
        ).fetchone()[0]
        total_cost = 0.0
        if has_stats:
            cost_where = (
                f" WHERE {owner_predicate('conversation_id')}" if owner else ""
            )
            cost_row = conn.execute(
                f"SELECT COALESCE(SUM(cost), 0) AS cost FROM conversation_stats{cost_where}",
                conv_params,
            ).fetchone()
            total_cost = cost_row["cost"]
        return UsageSummary(
            total_conversations=row["n"],
            total_input_tokens=row["inp"],
            total_output_tokens=row["out"],
            total_cost=total_cost,
            total_cache_read_tokens=row["cread"],
            total_cache_creation_tokens=row["ccreate"],
        )
    finally:
        conn.close()


def get_usage_by_model(*, db_path: Path | None = None, owner: str | None = None) -> list[GroupUsage]:
    """Get token/cost breakdown grouped by model.

    When ``owner`` is set, scoped to conversations owned by that user_id;
    ``owner=None`` is unscoped, the single-tenant/local default.
    """
    from siftd.storage.sql_helpers import has_conversation_owners_table, owner_predicate
    from siftd.storage.sqlite import open_database

    path = db_path or default_db_path()
    conn = open_database(path, read_only=True)
    try:
        if owner and not has_conversation_owners_table(conn):
            return []
        owner_where = f" WHERE {owner_predicate('u.conversation_id')}" if owner else ""
        owner_params = [owner] if owner else []
        # One GROUP BY over the rollup: tokens AND cost from the same grain. The
        # old two-query form joined per-conversation cs.cost to per-response rows,
        # fanning each conversation's cost out once per response (the 290x cost
        # inflation). Here cost is summed at (conversation, model) grain — no fan.
        rows = conn.execute(
            # Canonical identity (v11 models.name) is the display + grouping key,
            # not the raw adapter spelling — else dated/dot-form spellings of one
            # model (claude-haiku-4.5 vs claude-haiku-4-5) split the ledger. Fall
            # back to raw_name then 'unknown' for any model that didn't canonicalize.
            "SELECT COALESCE(m.name, m.raw_name, 'unknown') AS name,"
            " COUNT(DISTINCT u.conversation_id) AS convs,"
            " COALESCE(SUM(u.input_tokens), 0) AS inp,"
            " COALESCE(SUM(u.output_tokens), 0) AS out,"
            # No COALESCE on cost: an all-unpriced model sums to NULL → cost=None,
            # rendered as "unknown" rather than a fabricated $0 (see GroupUsage).
            " SUM(u.cost) AS cost"
            " FROM usage_by_conv_model u"
            " LEFT JOIN models m ON m.id = u.model_id"
            f"{owner_where}"
            " GROUP BY COALESCE(m.name, m.raw_name, 'unknown')",
            owner_params,
        ).fetchall()
        results = [
            GroupUsage(
                name=r["name"], conversations=r["convs"],
                input_tokens=r["inp"], output_tokens=r["out"], cost=r["cost"],
            )
            for r in rows
        ]
        results.sort(key=lambda g: g.input_tokens + g.output_tokens, reverse=True)
        return results
    finally:
        conn.close()


def get_usage_by_workspace(*, db_path: Path | None = None, owner: str | None = None) -> list[GroupUsage]:
    """Get token/cost breakdown grouped by workspace.

    When ``owner`` is set, scoped to conversations owned by that user_id;
    ``owner=None`` is unscoped, the single-tenant/local default.
    """
    from siftd.storage.sql_helpers import has_conversation_owners_table, owner_predicate
    from siftd.storage.sqlite import open_database

    path = db_path or default_db_path()
    conn = open_database(path, read_only=True)
    try:
        if owner and not has_conversation_owners_table(conn):
            return []
        owner_where = f" WHERE {owner_predicate('c.id')}" if owner else ""
        owner_params = [owner] if owner else []
        # One GROUP BY: tokens AND cost from the rollup, joined up to workspace.
        # The conversation count stays over `conversations` (LEFT JOIN the rollup)
        # so a workspace's zero-response conversations still count. Cost is now
        # the rollup's per-(conversation,model) cost summed per workspace — the
        # same correct per-model-cost primitive get_usage_by_model uses (shares
        # one definition; differs from the old per-conversation-rounded cs.cost
        # sum only by sub-cent rounding).
        rows = conn.execute(
            "SELECT COALESCE(w.path, '') AS name,"
            " COUNT(DISTINCT c.id) AS convs,"
            " COALESCE(SUM(u.input_tokens), 0) AS inp,"
            " COALESCE(SUM(u.output_tokens), 0) AS out,"
            # No COALESCE on cost: a workspace with no priced usage sums to NULL →
            # cost=None ("unknown"), never a fabricated $0 (see GroupUsage).
            " SUM(u.cost) AS cost"
            " FROM conversations c"
            " LEFT JOIN usage_by_conv_model u ON u.conversation_id = c.id"
            " LEFT JOIN workspaces w ON w.id = c.workspace_id"
            f"{owner_where}"
            " GROUP BY w.path",
            owner_params,
        ).fetchall()
        results = [
            GroupUsage(
                name=r["name"], conversations=r["convs"],
                input_tokens=r["inp"], output_tokens=r["out"], cost=r["cost"],
            )
            for r in rows
        ]
        # Sort by total tokens (not cost) so row order matches the token-sized
        # bars the dashboard draws — the same key get_usage_by_model uses, which
        # keeps bar length monotonic with rank. Ordering by cost let a high-token
        # cheap workspace sink below a low-token expensive one, so the bars read
        # as non-descending against the row order.
        results.sort(key=lambda g: g.input_tokens + g.output_tokens, reverse=True)
        return results
    finally:
        conn.close()


@dataclass
class Bucket:
    """One time-bucket of usage: a label plus summed tokens and honest cost.

    ``cost`` is ``None`` when no row in the bucket was priced (the GroupUsage
    rule), never a fabricated ``0.0`` — so the cost measure can render ``&mdash;``
    for an idle/unpriced bucket instead of a false zero.
    """

    label: str
    tokens: int
    cost: float | None


@dataclass
class UsageDistributions:
    """Activity over time: the daily series + hour-of-day and day-of-week rhythms.

    All three are projections of ``usage_by_conv_model`` joined to the
    conversation's start time (``localtime``). Tokens attribute to the
    conversation's START day/hour — the same coarse grain the Sessions daybook
    groups by; finer per-event attribution would need the rollup re-keyed by
    event, which it isn't (a 0.11.0 substrate concern).

    ``by_day`` is gap-filled (every calendar day in ``[first, last]`` present,
    zeroed where idle) so the trend reads as real elapsed time. ``by_hour`` (24,
    ``00``..``23``) and ``by_dow`` (7, ``Mon``..``Sun``) are dense by
    construction. Empty corpus → all three empty / all-zero.
    """

    by_day: list[Bucket]
    by_hour: list[Bucket]
    by_dow: list[Bucket]


@dataclass
class InputEconomy:
    """The input token economy — how the (true-total) input splits into freshly
    paid (uncached), cheaply re-served (cache reads), and one-time written
    (cache creation) tokens. ``input_tokens`` is the rollup's TRUE TOTAL, so
    ``uncached = input - cache_read - cache_creation``. Owner- and model-
    scopable, so the reckoning can show it for the whole corpus or one brushed
    model.
    """

    input_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int

    @property
    def uncached_tokens(self) -> int:
        return max(0, self.input_tokens - self.cache_read_tokens - self.cache_creation_tokens)

    @property
    def cache_hit_pct(self) -> float:
        return (self.cache_read_tokens / self.input_tokens * 100) if self.input_tokens else 0.0

    @property
    def has_cache(self) -> bool:
        return self.input_tokens > 0 and (self.cache_read_tokens + self.cache_creation_tokens) > 0


def get_input_economy(
    *,
    db_path: Path | None = None,
    owner: str | None = None,
    model_name: str | None = None,
) -> InputEconomy:
    """Input-token economy over the rollup (the reckoning's cache lever).

    Three SUMs over ``usage_by_conv_model``: true-total input + the two broken-
    out cache components. ``owner`` scopes to a tenant; ``model_name`` scopes to
    one canonical model (the chart-brushing — same COALESCE key as the other
    reads) so the strip can follow the Model-mix selection.
    """
    from siftd.storage.sql_helpers import has_conversation_owners_table, owner_predicate

    path = db_path or default_db_path()
    conn = open_database(path, read_only=True)
    try:
        if owner and not has_conversation_owners_table(conn):
            return InputEconomy(0, 0, 0)
        clauses: list[str] = []
        params: list = []
        if owner:
            clauses.append(owner_predicate("u.conversation_id"))
            params.append(owner)
        model_join = ""
        if model_name is not None:
            model_join = " LEFT JOIN models m ON m.id = u.model_id"
            clauses.append("COALESCE(m.name, m.raw_name, 'unknown') = ?")
            params.append(model_name)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        row = conn.execute(
            "SELECT COALESCE(SUM(u.input_tokens), 0) AS inp,"
            " COALESCE(SUM(u.cache_read_tokens), 0) AS cread,"
            " COALESCE(SUM(u.cache_creation_tokens), 0) AS ccreate"
            f" FROM usage_by_conv_model u{model_join}{where}",
            params,
        ).fetchone()
        return InputEconomy(row["inp"], row["cread"], row["ccreate"])
    finally:
        conn.close()


_DOW_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _fill_days(rows: list) -> list[Bucket]:
    """Gap-fill a sparse ``date -> (tok, cost)`` series into a dense daily run.

    Every calendar day between the first and last active day gets a bucket;
    idle days are zeroed (cost ``None``, not ``0`` — they were never priced).
    """
    from datetime import date, timedelta

    present = {r["k"]: r for r in rows if r["k"]}
    if not present:
        return []
    keys = sorted(present)
    start, end = date.fromisoformat(keys[0]), date.fromisoformat(keys[-1])
    out: list[Bucket] = []
    d = start
    while d <= end:
        key = d.isoformat()
        r = present.get(key)
        out.append(Bucket(key, r["tok"] if r else 0, r["cost"] if r else None))
        d += timedelta(days=1)
    return out


def get_usage_distributions(
    *,
    db_path: Path | None = None,
    owner: str | None = None,
    workspace_id: str | None = None,
    model_name: str | None = None,
) -> UsageDistributions:
    """Daily / hourly / weekday token+cost distributions over the rollup.

    Three GROUP BYs over ``usage_by_conv_model`` joined up to
    ``conversations.started_at`` (no schema, no new fact — the dashboard
    reckoning's activity charts and the per-workspace cadence strip are both
    projections of this). ``workspace_id`` scopes to one workspace (the cadence
    strip); ``model_name`` scopes to one canonical model (the reckoning's
    chart-brushing — same COALESCE(name, raw_name, 'unknown') grouping key
    get_usage_by_model ranks by); ``owner`` scopes to a tenant, matching every
    other read here.
    """
    from siftd.storage.sql_helpers import has_conversation_owners_table, owner_predicate

    empty_hours = [Bucket(f"{h:02d}", 0, None) for h in range(24)]
    empty_dows = [Bucket(label, 0, None) for label in _DOW_LABELS]

    path = db_path or default_db_path()
    conn = open_database(path, read_only=True)
    try:
        if owner and not has_conversation_owners_table(conn):
            return UsageDistributions([], empty_hours, empty_dows)

        clauses: list[str] = []
        params: list = []
        if owner:
            clauses.append(owner_predicate("c.id"))
            params.append(owner)
        if workspace_id:
            clauses.append("c.workspace_id = ?")
            params.append(workspace_id)
        # The model join is only needed when brushing by model; keep it out of the
        # unscoped query so the common path stays a two-table join.
        model_join = ""
        if model_name is not None:
            model_join = " LEFT JOIN models m ON m.id = u.model_id"
            clauses.append("COALESCE(m.name, m.raw_name, 'unknown') = ?")
            params.append(model_name)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        def run(grain: str) -> list:
            # tokens = the rollup's TRUE-TOTAL input + output (same as every other
            # GROUP BY here); cost left un-COALESCEd so an unpriced bucket sums to
            # NULL → Bucket.cost None, never a fabricated $0.
            return conn.execute(
                f"SELECT {grain} AS k,"
                " COALESCE(SUM(u.input_tokens + u.output_tokens), 0) AS tok,"
                " SUM(u.cost) AS cost"
                " FROM usage_by_conv_model u"
                " JOIN conversations c ON c.id = u.conversation_id"
                f"{model_join}{where}"
                " GROUP BY k",
                params,
            ).fetchall()

        day_rows = run("date(c.started_at, 'localtime')")
        hour_rows = run("strftime('%H', c.started_at, 'localtime')")
        dow_rows = run("strftime('%w', c.started_at, 'localtime')")
    finally:
        conn.close()

    by_day = _fill_days(day_rows)

    hour_present = {r["k"]: r for r in hour_rows if r["k"] is not None}
    by_hour = [
        Bucket(f"{h:02d}", (r["tok"] if (r := hour_present.get(f"{h:02d}")) else 0),
               r["cost"] if r else None)
        for h in range(24)
    ]

    # SQLite %w is 0=Sunday..6=Saturday; remap to 0=Mon..6=Sun for the rhythm.
    dow_present: dict[int, dict] = {}
    for r in dow_rows:
        if r["k"] is None:
            continue
        dow_present[(int(r["k"]) + 6) % 7] = r
    by_dow = [
        Bucket(_DOW_LABELS[i], (r["tok"] if (r := dow_present.get(i)) else 0),
               r["cost"] if r else None)
        for i in range(7)
    ]

    return UsageDistributions(by_day, by_hour, by_dow)


@dataclass
class WorkspaceDetail:
    """Per-workspace detail, keyed by the workspace ULID.

    Composes the stat grid + a by-model breakdown within the workspace +
    recent conversations. ``model_mix`` reuses :class:`GroupUsage` (one row per
    model, scoped to this workspace). All counts are owner-scoped when ``owner``
    is set. ``top_tags`` is deferred (needs its own tag-by-workspace query).
    """

    id: str
    path: str
    git_remote: str | None
    sessions: int
    input_tokens: int
    output_tokens: int
    cost: float | None
    model_mix: list[GroupUsage]
    recent: list
    cadence: list[Bucket]
    tags: list[tuple[str, int]]


def workspace_detail(
    workspace_id: str,
    *,
    fidelity,
    db_path: Path | None = None,
    owner: str | None = None,
    recent_n: int = 10,
) -> WorkspaceDetail | None:
    """Detail for one workspace, addressed by its stable ULID (workspaces.id).

    Mirrors the conversations list+detail split: the master list comes from
    :func:`list_workspaces`, this is the per-entity detail. Returns ``None`` if
    no workspace has that id (or, with ``owner`` set, if the owners table is
    absent — pre-migration safety, matching the other read fns).

    ``owner=None`` is unscoped (single-tenant/local default).
    """
    from siftd.api.conversations import list_conversations
    from siftd.storage.sql_helpers import has_conversation_owners_table, owner_predicate

    path = db_path or default_db_path()
    conn = open_database(path, read_only=True)
    try:
        if owner and not has_conversation_owners_table(conn):
            return None
        ws = conn.execute(
            "SELECT id, path, git_remote FROM workspaces WHERE id = ?",
            (workspace_id,),
        ).fetchone()
        if ws is None:
            return None

        owner_clause = f" AND {owner_predicate('u.conversation_id')}" if owner else ""
        owner_params = [owner] if owner else []
        # Model mix from the rollup: tokens AND cost per model within this
        # workspace. Cost was previously hardcoded 0.0 here (a self-contradicting
        # payload next to a real headline) — it is now the rollup's real
        # per-(conversation,model) cost.
        model_rows = conn.execute(
            # Canonical identity (v11 models.name) is the display + grouping key,
            # not the raw adapter spelling — else dated/dot-form spellings of one
            # model (claude-haiku-4.5 vs claude-haiku-4-5) split the ledger. Fall
            # back to raw_name then 'unknown' for any model that didn't canonicalize.
            "SELECT COALESCE(m.name, m.raw_name, 'unknown') AS name,"
            " COUNT(DISTINCT u.conversation_id) AS convs,"
            " COALESCE(SUM(u.input_tokens), 0) AS inp,"
            " COALESCE(SUM(u.output_tokens), 0) AS out,"
            # No COALESCE on cost: an all-unpriced model sums to NULL → cost=None,
            # rendered "unknown" not a fabricated $0 (the GroupUsage rule the
            # dashboard already honors; this is the detail twin that lagged it).
            " SUM(u.cost) AS cost"
            " FROM usage_by_conv_model u"
            " JOIN conversations c ON c.id = u.conversation_id"
            " LEFT JOIN models m ON m.id = u.model_id"
            " WHERE c.workspace_id = ?"
            f"{owner_clause}"
            " GROUP BY COALESCE(m.name, m.raw_name, 'unknown')",
            [workspace_id, *owner_params],
        ).fetchall()
        model_mix = [
            GroupUsage(
                name=r["name"], conversations=r["convs"],
                input_tokens=r["inp"], output_tokens=r["out"], cost=r["cost"],
            )
            for r in model_rows
        ]
        model_mix.sort(key=lambda g: g.input_tokens + g.output_tokens, reverse=True)

        # Sessions scoped to this workspace (owner-aware). Count stays over
        # `conversations` (not the rollup) so a conversation with zero responses
        # still counts as a session.
        conv_owner = f" AND {owner_predicate('c.id')}" if owner else ""
        sessions = conn.execute(
            "SELECT COUNT(*) FROM conversations c WHERE c.workspace_id = ?" + conv_owner,
            [workspace_id, *owner_params],
        ).fetchone()[0]

        # Cross-tenant read IDOR guard: an owner-scoped caller may only see a
        # workspace they actually participate in. Sibling fetch_top_workspaces
        # makes a workspace visible iff the owner owns >=1 conversation there;
        # mirror that here so a foreign workspace ULID returns None (404 at the
        # route) instead of leaking the workspace's path + private git_remote.
        # owner=None (single-tenant/local) stays fully unscoped.
        if owner and sessions == 0:
            return None

        # "What it's about": conversation-level tags on this workspace's
        # conversations, counted by conversation (the subject-index grain), most
        # used first. Owner-scoped via the conversation, same as the rest.
        tag_rows = conn.execute(
            "SELECT t.name AS name, COUNT(DISTINCT ta.target_id) AS n"
            " FROM tag_assignments ta"
            " JOIN tags t ON t.id = ta.tag_id"
            " JOIN conversations c ON c.id = ta.target_id"
            " WHERE ta.target_kind = 'conversation' AND c.workspace_id = ?"
            f"{conv_owner}"
            " GROUP BY t.name"
            " ORDER BY n DESC, t.name"
            " LIMIT 12",
            [workspace_id, *owner_params],
        ).fetchall()
        tags = [(r["name"], r["n"]) for r in tag_rows]
    finally:
        conn.close()

    # Cadence: the reckoning's daily trend scoped to this one workspace (its own
    # GROUP BY over the rollup — see get_usage_distributions).
    cadence = get_usage_distributions(
        db_path=path, owner=owner, workspace_id=workspace_id
    ).by_day

    recent = list_conversations(
        fidelity=fidelity, db_path=path, workspace_id=ws["id"], owner=owner, n=recent_n,
    )
    # Headline cost is the sum of the model mix — one source, so the headline
    # can never disagree with the per-model rows shown beneath it. It stays None
    # (not a fabricated $0) when no model in the workspace has priced usage,
    # matching the dashboard headline; a genuine summed $0 with priced rows
    # present is distinct from "unknown".
    priced = [g.cost for g in model_mix if g.cost is not None]
    return WorkspaceDetail(
        id=ws["id"],
        path=ws["path"],
        git_remote=ws["git_remote"],
        sessions=sessions,
        input_tokens=sum(g.input_tokens for g in model_mix),
        output_tokens=sum(g.output_tokens for g in model_mix),
        cost=sum(priced) if priced else None,
        model_mix=model_mix,
        recent=recent,
        cadence=cadence,
        tags=tags,
    )
